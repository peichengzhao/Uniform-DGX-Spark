from __future__ import annotations

import threading
import time
from pathlib import Path

from ..schemas import DeploymentRead
from ..state import TERMINAL_STATUSES, DeploymentStatus, InvalidTransition
from ..store import DeploymentNotFound, DeploymentStore
from .compatibility import analyze_model_directory
from .docker_runtime import DockerRuntime
from .downloads import HuggingFaceDownloadManager
from .modelscope_downloads import ModelScopeDownloadManager


class DeploymentWorker:
    def __init__(
        self,
        store: DeploymentStore,
        runtime: DockerRuntime,
        download_manager: HuggingFaceDownloadManager | None = None,
        modelscope_download_manager: ModelScopeDownloadManager | None = None,
        *,
        poll_seconds: float = 0.5,
        default_startup_timeout_seconds: int = 600,
    ) -> None:
        self.store = store
        self.runtime = runtime
        self.download_manager = download_manager
        self.modelscope_download_manager = modelscope_download_manager
        self.poll_seconds = poll_seconds
        self.default_startup_timeout_seconds = default_startup_timeout_seconds
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.runtime.check_available()
        self.reconcile_running()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="uniform-deployment-worker",
            daemon=True,
        )
        self._thread.start()

    def reconcile_running(self) -> None:
        for deployment in self.store.list_by_status([DeploymentStatus.RUNNING], limit=200):
            if self.runtime.is_running(deployment.id) and self.runtime.health(
                deployment.parameters.port
            ):
                continue
            self.store.transition(
                deployment.id,
                DeploymentStatus.FAILED,
                message="Previously running container is missing or unhealthy",
                event_type="runtime_reconciliation_failed",
                error_code="runtime_missing",
                error_message="Container was not healthy when the control service restarted",
            )

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread:
            self._thread.join(timeout=10)

    def wake(self) -> None:
        self._wake_event.set()

    def request_cancel(self, deployment_id: str) -> DeploymentRead:
        deployment = self.store.cancel(deployment_id)
        if self.runtime.exists(deployment_id):
            self.runtime.stop(deployment_id, remove=True)
        self.wake()
        return deployment

    def request_stop(self, deployment_id: str) -> DeploymentRead:
        deployment = self.store.get(deployment_id)
        if deployment.status == DeploymentStatus.RUNNING:
            deployment = self.store.transition(
                deployment_id,
                DeploymentStatus.STOPPING,
                message="Stop requested by user",
                event_type="stop_requested",
            )
            self.wake()
            return deployment
        if deployment.status in TERMINAL_STATUSES:
            return deployment
        return self.request_cancel(deployment_id)

    def request_retry(self, deployment_id: str) -> DeploymentRead:
        deployment = self.store.get(deployment_id)
        if deployment.status not in TERMINAL_STATUSES:
            raise InvalidTransition(f"cannot retry deployment while status is {deployment.status}")
        retried = self.store.transition(
            deployment_id,
            DeploymentStatus.QUEUED,
            message="Deployment queued for retry",
            event_type="retry_requested",
            progress=0,
        )
        self.wake()
        return retried

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            stopping = self.store.list_by_status([DeploymentStatus.STOPPING], limit=1)
            if stopping:
                self._process_stop(stopping[0])
                continue
            queued = self.store.list_by_status([DeploymentStatus.QUEUED], limit=1)
            if queued:
                self._process_deployment(queued[0])
                continue
            self._wake_event.wait(self.poll_seconds)
            self._wake_event.clear()

    def _process_stop(self, deployment: DeploymentRead) -> None:
        try:
            self.runtime.stop(deployment.id, remove=False)
            self.store.transition(
                deployment.id,
                DeploymentStatus.STOPPED,
                message="vLLM container stopped",
                progress=1,
            )
        except Exception as error:  # noqa: BLE001 - persisted task boundary
            self._fail(deployment.id, "container_stop_failed", error)

    def _process_deployment(self, deployment: DeploymentRead) -> None:
        try:
            deployment = self._transition(
                deployment.id,
                DeploymentStatus.PREFLIGHT,
                "Checking Docker, model path, port, and image configuration",
                0.05,
            )
            self.runtime.check_available()
            self.runtime.remove_if_managed(deployment.id)
            self.runtime.ensure_port_available(deployment.parameters.port)

            deployment = self._transition(
                deployment.id,
                DeploymentStatus.DOWNLOADING_MODEL,
                "Resolving model files",
                0.1,
            )
            model_path = self._resolve_model_path(deployment)
            self._ensure_active(deployment.id)
            compatibility = analyze_model_directory(model_path)

            deployment = self._transition(
                deployment.id,
                DeploymentStatus.PULLING_IMAGE,
                (
                    f"{compatibility.architecture}; "
                    f"weights={compatibility.weight_bytes} bytes; "
                    f"memory estimate={compatibility.estimated_peak_bytes} bytes"
                ),
                0.55,
            )
            self.runtime.ensure_image(
                deployment.parameters.image,
                progress_callback=lambda line: self.store.record_event(
                    deployment.id,
                    "image_pull_output",
                    line[:1000],
                    details={"image": deployment.parameters.image},
                ),
                cancel_check=lambda: (
                    self.store.get(deployment.id).status == DeploymentStatus.CANCELLED
                ),
            )
            self._ensure_active(deployment.id)

            deployment = self._transition(
                deployment.id,
                DeploymentStatus.STARTING_CONTAINER,
                "Creating the managed vLLM container",
                0.65,
            )
            self.runtime.start(deployment, model_path)

            deployment = self._transition(
                deployment.id,
                DeploymentStatus.LOADING_MODEL,
                "Waiting for vLLM to load the model",
                0.75,
            )
            self._wait_until_healthy(deployment)
            self._ensure_active(deployment.id)

            deployment = self._transition(
                deployment.id,
                DeploymentStatus.HEALTH_CHECKING,
                "Running an OpenAI-compatible inference smoke test",
                0.95,
            )
            self.runtime.smoke_test(deployment)
            self._transition(
                deployment.id,
                DeploymentStatus.RUNNING,
                "vLLM service is ready",
                1.0,
            )
        except InvalidTransition:
            current = self.store.get(deployment.id)
            if current.status == DeploymentStatus.CANCELLED:
                if self.runtime.exists(deployment.id):
                    self.runtime.stop(deployment.id, remove=True)
                return
            raise
        except Exception as error:  # noqa: BLE001 - persisted task boundary
            self._fail(deployment.id, "deployment_failed", error)

    def _wait_until_healthy(self, deployment: DeploymentRead) -> None:
        timeout = (
            deployment.parameters.startup_timeout_seconds or self.default_startup_timeout_seconds
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._ensure_active(deployment.id)
            if not self.runtime.is_running(deployment.id):
                logs = self.runtime.logs(deployment.id, tail=80)
                raise RuntimeError(f"vLLM container exited during startup\n{logs[-4000:]}")
            if self.runtime.health(deployment.parameters.port):
                return
            self._stop_event.wait(1)
        logs = self.runtime.logs(deployment.id, tail=80)
        raise RuntimeError(f"vLLM startup timed out after {timeout}s\n{logs[-4000:]}")

    def _resolve_model_path(self, deployment: DeploymentRead) -> Path:
        configured = deployment.parameters.local_model_path
        if configured is not None:
            path = Path(configured).resolve()
            if not path.is_dir() or not (path / "config.json").is_file():
                raise RuntimeError(f"invalid local model directory: {path}")
            self.store.update_progress(deployment.id, 0.5)
            return path
        manager = (
            self.download_manager
            if deployment.parameters.source == "huggingface"
            else self.modelscope_download_manager
        )
        if manager is None:
            raise RuntimeError(f"{deployment.parameters.source} download manager is unavailable")

        def report_progress(fraction: float, current: int, total: int | None) -> None:
            del current, total
            self.store.update_progress(deployment.id, 0.1 + (fraction * 0.4))

        result = manager.download(
            deployment.model_id,
            deployment.revision,
            progress_callback=report_progress,
        )
        self.store.set_revision(deployment.id, result.revision)
        return result.path

    def _ensure_active(self, deployment_id: str) -> None:
        current = self.store.get(deployment_id)
        if current.status == DeploymentStatus.CANCELLED:
            raise InvalidTransition("deployment was cancelled")

    def _transition(
        self,
        deployment_id: str,
        target: DeploymentStatus,
        message: str,
        progress: float,
    ) -> DeploymentRead:
        return self.store.transition(
            deployment_id,
            target,
            message=message,
            progress=progress,
        )

    def _fail(self, deployment_id: str, code: str, error: Exception) -> None:
        try:
            current = self.store.get(deployment_id)
            if current.status in TERMINAL_STATUSES:
                return
            self.store.transition(
                deployment_id,
                DeploymentStatus.FAILED,
                message=str(error),
                event_type="failed",
                error_code=code,
                error_message=str(error),
            )
        except (DeploymentNotFound, InvalidTransition):
            return
