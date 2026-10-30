from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from uniform_dgxspark.database import Database
from uniform_dgxspark.schemas import DeploymentCreate, DeploymentRead
from uniform_dgxspark.services.worker import DeploymentWorker
from uniform_dgxspark.state import ALLOWED_TRANSITIONS, DeploymentStatus
from uniform_dgxspark.store import DeploymentStore


class FakeRuntime:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def check_available(self) -> None:
        pass

    def ensure_port_available(self, port: int) -> None:
        assert port == 18080

    def ensure_image(self, image: str, **kwargs: object) -> None:
        del kwargs
        assert image

    def remove_if_managed(self, deployment_id: str) -> None:
        self.started = False
        self.stopped = False

    def start(self, deployment: DeploymentRead, model_path: Path) -> str:
        self.started = True
        return "fake-container"

    def exists(self, deployment_id: str) -> bool:
        return self.started and not self.stopped

    def is_running(self, deployment_id: str) -> bool:
        return self.started and not self.stopped

    def health(self, port: int) -> bool:
        return True

    def smoke_test(self, deployment: DeploymentRead) -> None:
        pass

    def stop(self, deployment_id: str, *, remove: bool = False) -> None:
        self.stopped = True

    def logs(self, deployment_id: str, *, tail: int = 200) -> str:
        return ""


def test_worker_completes_local_deployment_and_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "uniform_dgxspark.services.compatibility.psutil.virtual_memory",
        lambda: SimpleNamespace(available=16 * 1024**3),
    )
    model_path = tmp_path / "model"
    model_path.mkdir()
    (model_path / "config.json").write_text(
        '{"architectures":["Qwen2ForCausalLM"],"model_type":"qwen2"}'
    )
    (model_path / "model.safetensors").write_bytes(b"weights")
    database = Database(tmp_path / "uniform.db")
    database.initialize()
    store = DeploymentStore(database)
    deployment = store.create(
        DeploymentCreate.model_validate(
            {
                "model_id": "local/test",
                "parameters": {
                    "port": 18080,
                    "local_model_path": str(model_path),
                },
            }
        )
    )
    runtime = FakeRuntime()
    worker = DeploymentWorker(store, runtime, poll_seconds=0.01)

    worker._process_deployment(deployment)
    running = store.get(deployment.id)

    assert running.status == DeploymentStatus.RUNNING
    assert running.progress == 1
    assert runtime.started is True

    stopping = worker.request_stop(deployment.id)
    assert stopping.status == DeploymentStatus.STOPPING
    worker._process_stop(stopping)

    assert store.get(deployment.id).status == DeploymentStatus.STOPPED
    assert runtime.stopped is True


def test_reconciliation_marks_missing_running_container_failed(tmp_path: Path) -> None:
    model_path = tmp_path / "model"
    model_path.mkdir()
    database = Database(tmp_path / "uniform.db")
    database.initialize()
    store = DeploymentStore(database)
    deployment = store.create(
        DeploymentCreate.model_validate(
            {
                "model_id": "local/test",
                "parameters": {"port": 18080, "local_model_path": str(model_path)},
            }
        )
    )
    current = deployment
    for target in [
        DeploymentStatus.PREFLIGHT,
        DeploymentStatus.DOWNLOADING_MODEL,
        DeploymentStatus.PULLING_IMAGE,
        DeploymentStatus.STARTING_CONTAINER,
        DeploymentStatus.LOADING_MODEL,
        DeploymentStatus.HEALTH_CHECKING,
        DeploymentStatus.RUNNING,
    ]:
        assert target in ALLOWED_TRANSITIONS[current.status]
        current = store.transition(current.id, target, message="test")
    runtime = FakeRuntime()
    worker = DeploymentWorker(store, runtime)

    worker.reconcile_running()

    failed = store.get(deployment.id)
    assert failed.status == DeploymentStatus.FAILED
    assert failed.error_code == "runtime_missing"
