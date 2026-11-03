from __future__ import annotations

import asyncio
import faulthandler
import hashlib
import hmac
import json
import logging
import os
import re
import signal
import socket
import sqlite3
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, status
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect

from .accuracy_store import AccuracyRunNotFound, AccuracyRunStore
from .ai_accuracy_api import register_ai_accuracy_routes
from .ai_store import (
    AIDeploymentResultNotFound,
    AIDeploymentReuseConflict,
    AIDeploymentReusePending,
    AIDeploymentTemplateNotFound,
    AISessionNotFound,
    AISessionStore,
)
from .arena_store import ArenaRunNotFound, ArenaRunStore
from .benchmark_store import BenchmarkNotFound, BenchmarkStore
from .catalog import CatalogError, CatalogService, CatalogStore, merge_catalog_models
from .config import Settings
from .database import Database
from .dataset_integration_api import register_dataset_integration_routes
from .feishu_api import register_feishu_routes
from .gateway_store import (
    GatewayConsumer,
    GatewayConsumerNotFound,
    GatewayPublicationScopeInvalid,
    GatewayStore,
    GatewayToken,
    GatewayTokenNotFound,
)
from .node_store import SSHNodeAlreadyExists, SSHNodeStore
from .publication_store import ServicePublicationStore
from .schemas import (
    AccuracyArtifactList,
    AccuracyCompareCreate,
    AccuracyComparisonRead,
    AccuracyLogRead,
    AccuracyRunCreate,
    AccuracyRunList,
    AccuracyRunRead,
    AIAuditEventList,
    AIAutoResumeRead,
    AIAutoResumeUpdate,
    AIConversationMessageCreate,
    AIConversationRead,
    AIDeployedServiceList,
    AIDeployedServiceRead,
    AIDeploymentAction,
    AIDeploymentResultCreate,
    AIDeploymentResultRead,
    AIDeploymentReuseRead,
    AIDeploymentReuseRequest,
    AIDeploymentTemplateCreate,
    AIDeploymentTemplateRead,
    AIHealthCheckResult,
    AIInferenceTestResult,
    AIProviderRead,
    AISessionCreate,
    AISessionList,
    AISessionRead,
    AISessionResume,
    ArenaLeaderboardDetail,
    ArenaLeaderboardList,
    ArenaRunCreate,
    ArenaRunList,
    ArenaRunRead,
    ArenaRuntimeProfile,
    BenchmarkCreate,
    BenchmarkList,
    BenchmarkRead,
    CatalogModelList,
    CatalogModelRead,
    CatalogRefreshRead,
    CatalogRemoteSearchRead,
    DeploymentCreate,
    DeploymentEventList,
    DeploymentList,
    DeploymentParameters,
    DeploymentRead,
    GatewayConsumerCreate,
    GatewayConsumerList,
    GatewayConsumerRead,
    GatewayTokenCreate,
    GatewayTokenRead,
    GatewayTokenSecret,
    GatewayUsagePage,
    GatewayUsageRead,
    GatewayUsageSummary,
    HealthRead,
    LocalImageList,
    LocalModelInventory,
    LocalModelRootCreate,
    LocalModelRootRead,
    ServicePublicationRead,
    ServicePublicationSecret,
    SSHNodeCreate,
    SSHNodeRead,
    UIEditConversationRead,
    UIEditCreate,
    UIEditList,
    UIEditLogRead,
    UIEditMessageCreate,
    UIEditorStatus,
    UIEditRead,
    UIEditRevertCreate,
    UIPageCaptureCreate,
)
from .services.accuracy import (
    AccuracyBusy,
    AccuracyCompatibilityError,
    AccuracyManager,
    AccuracyTargetError,
)
from .services.ai_accuracy import AIAccuracyManager
from .services.ai_terminal import AIProviderUnavailable, AISessionBusy, AITerminalManager
from .services.arena import (
    ArenaBusy,
    ArenaLeaderboardError,
    ArenaManager,
    ArenaTargetError,
    prepare_log_for_analysis,
)
from .services.benchmarking import BenchmarkBusy, BenchmarkManager, BenchmarkTargetError
from .services.catalog_refresh import CatalogRefreshWorker
from .services.cluster_monitoring import ClusterMonitoringService
from .services.dataset_integration import DatasetIntegrationManager
from .services.deployment_templates import (
    AIDeploymentTemplateManager,
    DeploymentTemplateBusy,
    DeploymentTemplateExecutionError,
    DeploymentTemplateInvalid,
)
from .services.docker_runtime import DockerCommandError, DockerRuntime, RuntimeErrorBase
from .services.downloads import HuggingFaceDownloadManager
from .services.evalscope_accuracy import EvalScopeAccuracyAdapter, EvalScopeAccuracyError
from .services.external_access import (
    ExternalAccessStartError,
    ExternalAccessUnavailable,
    ServiceExternalAccessManager,
)
from .services.feishu_notifications import FeishuNotifications
from .services.local_models import LocalModelRootError, LocalModelService
from .services.model_targets import ModelTargetError
from .services.modelscope_downloads import ModelScopeDownloadManager
from .services.monitoring import MetricsService
from .services.publication import (
    PublicationAlreadyEnabled,
    PublicationGatewayUnavailable,
    PublicationNotEnabled,
    PublicationStatus,
    PublicationTargetNotFound,
    PublicationTargetUnavailable,
    ServicePublicationManager,
)
from .services.publisher_icons import PublisherIconService
from .services.references import SafeReferenceFetcher
from .services.ui_capture import (
    UIPageCaptureError,
    UIPageCaptureService,
    UIPageCaptureUnavailable,
)
from .services.ui_editing import (
    UIEditBusy,
    UIEditConflict,
    UIEditManager,
    UIEditorUnavailable,
)
from .services.worker import DeploymentWorker
from .state import DeploymentStatus, InvalidTransition
from .store import DeploymentNotFound, DeploymentStore
from .ui_edit_store import UIEditNotFound, UIEditStore

LOGGER = logging.getLogger(__name__)

_UNSAFE_DOWNLOAD_FILENAME = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]+')


def _configure_service_logging() -> None:
    """Make control-service progress visible in the journal when run standalone."""
    logger = logging.getLogger("uniform_dgxspark")
    if logger.handlers:
        return
    level = getattr(logging, os.getenv("UNIFORM_LOG_LEVEL", "INFO").upper(), logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


def _install_stack_dump_handler() -> None:
    """Allow on-demand thread dumps through ``kill -USR1 <pid>``."""
    register = getattr(faulthandler, "register", None)
    if register is None:
        return
    try:
        register(signal.SIGUSR1, chain=False, all_threads=True)
    except (AttributeError, OSError, ValueError):
        LOGGER.debug("could not register the startup stack dump handler", exc_info=True)


def _start_startup_watchdog(seconds: float) -> threading.Timer | None:
    """Dump every thread's stack when initialization takes suspiciously long."""
    if seconds <= 0:
        return None

    def dump() -> None:
        LOGGER.warning(
            "application startup is still running after %.0f seconds; dumping thread stacks",
            seconds,
        )
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)

    timer = threading.Timer(seconds, dump)
    timer.daemon = True
    timer.start()
    return timer


def _run_startup_step_in_background(label: str, callback: Callable[[], object]) -> None:
    """Run a slow, optional startup step without blocking the HTTP listener."""

    def runner() -> None:
        started = time.monotonic()
        try:
            callback()
        except Exception:
            LOGGER.exception("background startup step %s failed", label)
            return
        LOGGER.info(
            "background startup step %s finished in %.1fs", label, time.monotonic() - started
        )

    threading.Thread(
        target=runner,
        name=f"uniform-startup-{label}",
        daemon=True,
    ).start()


def _start_deployment_worker_in_background(worker: DeploymentWorker) -> None:
    """Keep retrying so a temporarily missing Docker daemon cannot disable the queue."""

    def runner() -> None:
        delay = 5.0
        while True:
            try:
                worker.start()
            except Exception:
                LOGGER.exception(
                    "deployment worker could not start; retrying in %.0f seconds", delay
                )
                time.sleep(delay)
                delay = min(delay * 2, 60.0)
                continue
            LOGGER.info("background startup step deployment-worker finished")
            return

    threading.Thread(
        target=runner,
        name="uniform-startup-deployment-worker",
        daemon=True,
    ).start()


def _performance_report_download_filename(session: AISessionRead) -> str:
    """Build a stable, filesystem-safe user-facing name for a session report."""
    session_name = _UNSAFE_DOWNLOAD_FILENAME.sub("_", session.display_name.strip())
    session_name = re.sub(r"\s+", "_", session_name)
    session_name = re.sub(r"_+", "_", session_name).strip(" ._") or "AI会话"
    # Leave room for the timestamp, suffix and extension under common 255-byte limits.
    session_name = session_name.encode("utf-8")[:180].decode("utf-8", errors="ignore")
    session_name = session_name.rstrip(" ._") or "AI会话"
    created_at = session.created_at.astimezone().strftime("%Y-%m-%d_%H%M")
    return f"{session_name}_{created_at}_综合报告.md"


def _allowed_publication_control_hosts(settings: Settings) -> set[str]:
    hosts = {
        "127.0.0.1",
        "::1",
        "localhost",
        socket.gethostname().casefold().rstrip("."),
        socket.getfqdn().casefold().rstrip("."),
    }
    hosts.update(settings.publication_control_hosts)
    if settings.publication_advertise_host:
        hosts.add(settings.publication_advertise_host.casefold().strip("[]").rstrip("."))
    try:
        hosts.add(ServicePublicationManager._discover_lan_host().casefold().rstrip("."))
    except OSError:
        pass
    return {host for host in hosts if host}


def _deployment_with_live_status(
    deployment: DeploymentRead, runtime: DockerRuntime
) -> DeploymentRead:
    """Replace a cached running state with the currently observed service state."""
    if deployment.status != DeploymentStatus.RUNNING:
        return deployment
    container_running = runtime.is_running(deployment.id)
    if not container_running:
        return deployment.model_copy(
            update={
                "status": DeploymentStatus.STOPPED,
                "error_code": "runtime_container_missing",
                "error_message": "数据库记录为运行中，但对应容器当前未运行",
            }
        )
    if not runtime.health(deployment.parameters.port, timeout=0.5):
        return deployment.model_copy(
            update={
                "status": DeploymentStatus.FAILED,
                "error_code": "runtime_health_unreachable",
                "error_message": "容器仍在运行，但模型健康检查当前不可达",
            }
        )
    return deployment


_RECOVERED_SERVICE_PROMPT = (
    "Uniform recovered this deployment registration from a running Docker container "
    "after its original database record was unavailable."
)


def _recover_labeled_ai_services(
    ai_store: AISessionStore,
    runtime: DockerRuntime,
    sessions_dir: Path,
) -> None:
    """Restore missing DB registrations from trusted, currently running containers."""
    for discovered in runtime.discover_ai_services():
        workspace = (sessions_dir / discovered.session_id).resolve()
        if sessions_dir not in workspace.parents:
            continue
        try:
            workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            continue
        models = runtime.openai_models(discovered.port, timeout=0.5)
        model_name = models[0] if models else discovered.model_name
        health_reachable = runtime.health(discovered.port, timeout=0.5)
        models_reachable = bool(models)
        try:
            session = ai_store.get(discovered.session_id)
        except AISessionNotFound:
            try:
                session = ai_store.create(
                    AISessionCreate(
                        provider="codex",
                        task_type="deployment",
                        display_name=discovered.display_name,
                        execution_mode="autonomous",
                        initial_prompt=_RECOVERED_SERVICE_PROMPT,
                    ),
                    str(workspace),
                    session_id=discovered.session_id,
                    display_name=discovered.display_name,
                    target_model_id=model_name,
                )
                session = ai_store.update_status(
                    session.id,
                    "exited",
                    message="Recovered the session record from its running labeled container",
                )
                ai_store.add_audit(
                    session.id,
                    "service_registration_recovered",
                    "Recovered a missing service registration from Docker labels",
                    {
                        "container_name": discovered.container_name,
                        "container_id": discovered.container_id,
                        "port": discovered.port,
                    },
                )
            except sqlite3.IntegrityError:
                # Two browser refreshes may reconcile the same container concurrently.
                session = ai_store.get(discovered.session_id)
        try:
            registered = ai_store.get_result(discovered.session_id)
        except AIDeploymentResultNotFound:
            registered = None
        if registered is not None and (
            registered.container_name == discovered.container_name
            or runtime.is_named_container_running(registered.container_name)
        ):
            continue
        service_url = f"http://127.0.0.1:{discovered.port}"
        ai_store.upsert_result(
            discovered.session_id,
            AIDeploymentResultCreate(
                engine=discovered.engine,
                container_name=discovered.container_name,
                container_id=discovered.container_id,
                image=discovered.image,
                port=discovered.port,
                service_url=service_url,
                launch_command=discovered.launch_command,
                health_check=AIHealthCheckResult(
                    method="GET",
                    url=f"{service_url}/health",
                    status_code=200 if health_reachable else None,
                    passed=health_reachable,
                    details="Recovered from the currently running labeled container.",
                ),
                inference_test=AIInferenceTestResult(
                    method="GET",
                    url=f"{service_url}/v1/models",
                    status_code=200 if models_reachable else None,
                    passed=models_reachable,
                    request_summary="OpenAI-compatible model discovery",
                    response_summary=(
                        f"Available models: {', '.join(models)}"
                        if models
                        else "The model discovery endpoint was not reachable."
                    ),
                ),
            ),
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or Settings.from_environment()
    publication_control_hosts = _allowed_publication_control_hosts(resolved_settings)
    _configure_service_logging()
    _install_stack_dump_handler()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        startup_watchdog = _start_startup_watchdog(resolved_settings.startup_watchdog_seconds)
        database = Database(resolved_settings.database_path)
        database.initialize()
        LOGGER.info("startup: database ready at %s", resolved_settings.database_path)
        store = DeploymentStore(database)
        catalog_store = CatalogStore(database)
        ai_store = AISessionStore(database)
        ui_edit_store = UIEditStore(database)
        benchmark_store = BenchmarkStore(database)
        arena_store = ArenaRunStore(database, resolved_settings.arena_runs_dir)
        accuracy_store = AccuracyRunStore(database)
        publication_store = ServicePublicationStore(database)
        gateway_store = GatewayStore(database)
        ssh_node_store = SSHNodeStore(database)
        ai_store.recover_incomplete()
        benchmark_store.recover_incomplete()
        arena_store.recover_incomplete()
        accuracy_recovered = accuracy_store.recover_incomplete()
        local_models = LocalModelService(database)
        local_models.seed_roots(resolved_settings.initial_local_model_roots)
        LOGGER.info("startup: local model roots scanned")
        catalog_service = CatalogService(
            catalog_store,
            hf_token=resolved_settings.hf_token,
            modelscope_token=resolved_settings.modelscope_token,
        )
        publisher_icons = PublisherIconService()
        recovered = store.recover_incomplete() + accuracy_recovered
        LOGGER.info("startup: recovered %d interrupted operation(s)", recovered)
        runtime = DockerRuntime(docker_binary=resolved_settings.docker_binary)
        publication_manager = ServicePublicationManager(
            publication_store,
            store,
            ai_store,
            runtime,
            gateway_store,
            listen_host=resolved_settings.publication_host,
            listen_port=resolved_settings.publication_port,
            advertise_host=resolved_settings.publication_advertise_host,
            request_timeout_seconds=resolved_settings.publication_request_timeout_seconds,
        )
        external_access_manager = ServiceExternalAccessManager(
            resolved_settings.external_access_state_path,
            enabled=resolved_settings.external_access_enabled,
            cloudflared_binary=resolved_settings.cloudflared_binary,
            gateway_url=f"http://127.0.0.1:{resolved_settings.publication_port}",
            start_timeout_seconds=(resolved_settings.external_access_start_timeout_seconds),
        )
        download_manager = HuggingFaceDownloadManager(
            resolved_settings.models_dir,
            token=resolved_settings.hf_token,
            max_workers=resolved_settings.download_workers,
        )
        modelscope_download_manager = ModelScopeDownloadManager(
            resolved_settings.models_dir,
            token=resolved_settings.modelscope_token,
            max_workers=resolved_settings.download_workers,
        )
        worker = DeploymentWorker(
            store,
            runtime,
            download_manager,
            modelscope_download_manager,
            poll_seconds=resolved_settings.worker_poll_seconds,
            default_startup_timeout_seconds=(resolved_settings.default_startup_timeout_seconds),
        )
        metrics = MetricsService(
            resolved_settings.data_dir,
            interval_seconds=resolved_settings.metrics_interval_seconds,
        )
        cluster_monitoring = ClusterMonitoringService(
            ssh_node_store,
            interval_seconds=resolved_settings.ssh_monitor_interval_seconds,
        )
        ai_terminal = AITerminalManager(
            ai_store,
            resolved_settings.ai_sessions_dir,
            codex_binary=resolved_settings.codex_binary,
            claude_binary=resolved_settings.claude_binary,
            deepseek_binary=resolved_settings.deepseek_binary,
            deepseek_args=resolved_settings.deepseek_args,
            reference_fetcher=SafeReferenceFetcher(
                max_bytes=resolved_settings.ai_reference_max_bytes
            ),
            control_url=resolved_settings.control_url,
            benchmark_reference_dir=resolved_settings.benchmark_reference_dir,
            benchmark_history_csv=resolved_settings.benchmark_history_csv,
        )
        ai_deployment_templates = AIDeploymentTemplateManager(
            ai_store,
            resolved_settings.data_dir / "ai-deployment-templates",
            docker_binary=resolved_settings.docker_binary,
        )
        historical_candidates: list[
            tuple[bool, datetime, AISessionRead, AIDeploymentResultRead]
        ] = []
        for historical_result in ai_store.list_results(limit=10_000, include_archived=True):
            if historical_result.status != "verified":
                continue
            try:
                historical_session = ai_store.get(historical_result.session_id)
            except AISessionNotFound:
                continue
            # Backfill oldest/archived records first. Since template upserts receive a new
            # timestamp, processing newest active records last preserves the same precedence
            # as the deployment results instead of letting an old archived session win.
            historical_candidates.append(
                (
                    historical_session.archived_at is None,
                    historical_result.updated_at,
                    historical_session,
                    historical_result,
                )
            )
        historical_candidates.sort(key=lambda item: (item[0], item[1], item[2].id))
        for _, _, historical_session, historical_result in historical_candidates:
            try:
                historical_template = ai_store.get_template(historical_result.session_id)
                if historical_template.format_version >= ai_deployment_templates.FORMAT_VERSION:
                    # Touch an already-current template in the same ordered pass. Otherwise a
                    # newly backfilled older record would receive a newer database timestamp and
                    # incorrectly become the model's "latest" template.
                    ai_store.upsert_template(
                        AIDeploymentTemplateCreate.model_validate(
                            historical_template.model_dump(exclude={"created_at", "updated_at"})
                        )
                    )
                    continue
            except AIDeploymentTemplateNotFound:
                pass
            try:
                historical_deployment = None
                if historical_session.deployment_id:
                    try:
                        historical_deployment = store.get(historical_session.deployment_id)
                    except DeploymentNotFound:
                        pass
                ai_deployment_templates.create(
                    historical_session,
                    historical_result,
                    deployment_context=ai_terminal.model_context(historical_result.session_id),
                    deployment=historical_deployment,
                    historical=True,
                )
            except (
                AISessionNotFound,
                DeploymentTemplateInvalid,
                OSError,
                ValueError,
            ):
                # Historical sessions may predate durable workspaces or target metadata.
                continue
        ui_editor = UIEditManager(
            ui_edit_store,
            web_dir,
            resolved_settings.ui_edits_dir,
            codex_binary=resolved_settings.codex_binary,
            enabled=resolved_settings.ui_editing_enabled,
        )
        ui_page_capture = UIPageCaptureService(work_dir=resolved_settings.data_dir / "ui-captures")
        benchmark_manager = BenchmarkManager(benchmark_store, store, ai_store)
        arena_manager = ArenaManager(
            arena_store,
            store,
            ai_store,
            uvx_binary=resolved_settings.arena_uvx_binary,
            uv_cache_dir=resolved_settings.arena_uv_cache_dir,
            uv_tool_dir=resolved_settings.arena_uv_tool_dir,
            metrics_service=metrics,
            cluster_monitoring=cluster_monitoring,
        )
        accuracy_manager = AccuracyManager(
            accuracy_store,
            store,
            ai_store,
            resolved_settings.accuracy_runs_dir,
            EvalScopeAccuracyAdapter(
                resolved_settings.accuracy_evalscope_command,
                resolved_settings.accuracy_evalscope_source,
            ),
        )

        def accuracy_analysis_ready(session_id: str) -> None:
            try:
                session = ai_store.get(session_id)
                prompt = (
                    "评测已完成。调用 accuracy_status 读取真实指标，然后使用 "
                    "accuracy_record_analysis 保存简短中文解读，不要生成或修改分数。"
                )
                if session.status == "running":
                    ai_terminal.send_instruction(session_id, prompt)
                elif session.status == "exited":
                    ai_terminal.resume(session_id, prompt=prompt)
            except Exception:
                # Canonical results remain available when the AI provider is unavailable.
                pass

        ai_accuracy = AIAccuracyManager(
            database, accuracy_manager, ai_terminal, on_complete=accuracy_analysis_ready
        )
        ai_accuracy.recover()
        dataset_integrations = DatasetIntegrationManager(database, accuracy_manager, ai_terminal)
        dataset_integrations.recover()
        accuracy_manager.adapter.dataset_provider = dataset_integrations.approved_specs
        accuracy_manager.adapter.source_validator = lambda: (
            dataset_integrations.check_source_clean()
            if dataset_integrations.approved_specs()
            else None
        )
        catalog_refresh_worker = CatalogRefreshWorker(
            catalog_service,
            interval_seconds=resolved_settings.catalog_refresh_interval_seconds,
            limit=resolved_settings.catalog_refresh_limit,
        )
        application.state.database = database
        application.state.store = store
        application.state.recovered_tasks = recovered
        application.state.catalog_store = catalog_store
        application.state.catalog_service = catalog_service
        application.state.publisher_icons = publisher_icons
        application.state.runtime = runtime
        application.state.publication_store = publication_store
        application.state.publication_manager = publication_manager
        application.state.gateway_store = gateway_store
        application.state.external_access_manager = external_access_manager
        application.state.worker = worker
        application.state.metrics = metrics
        application.state.ssh_node_store = ssh_node_store
        application.state.cluster_monitoring = cluster_monitoring
        feishu_notifications = FeishuNotifications(
            database, resolved_settings.data_dir, metrics, cluster_monitoring
        )
        application.state.feishu_notifications = feishu_notifications
        application.state.ai_store = ai_store
        application.state.ai_terminal = ai_terminal
        application.state.ai_deployment_templates = ai_deployment_templates
        application.state.ai_service_stop_operations = {}
        application.state.ai_service_stop_lock = threading.Lock()
        application.state.ui_edit_store = ui_edit_store
        application.state.ui_editor = ui_editor
        application.state.ui_page_capture = ui_page_capture
        application.state.benchmark_store = benchmark_store
        application.state.benchmark_manager = benchmark_manager
        application.state.arena_store = arena_store
        application.state.arena_manager = arena_manager
        application.state.accuracy_store = accuracy_store
        application.state.accuracy_manager = accuracy_manager
        application.state.ai_accuracy = ai_accuracy
        application.state.dataset_integrations = dataset_integrations
        application.state.local_models = local_models
        application.state.catalog_refresh_worker = catalog_refresh_worker
        metrics.start()
        cluster_monitoring.start()
        feishu_notifications.start()
        _run_startup_step_in_background(
            "publication-gateway", publication_manager.start_for_existing_publications
        )
        _run_startup_step_in_background(
            "external-access",
            lambda: external_access_manager.restore_for_enabled_publications(
                publication_store.enabled_publication_ids()
            ),
        )
        if resolved_settings.worker_enabled:
            _start_deployment_worker_in_background(worker)
            if resolved_settings.catalog_auto_refresh_enabled:
                catalog_refresh_worker.start()
        if startup_watchdog is not None:
            startup_watchdog.cancel()
        LOGGER.info("application startup complete")
        try:
            yield
        finally:
            feishu_notifications.stop()
            dataset_integrations.stop_all()
            ai_accuracy.stop_all()
            external_access_manager.shutdown()
            publication_manager.stop_gateway()
            if resolved_settings.worker_enabled and resolved_settings.catalog_auto_refresh_enabled:
                catalog_refresh_worker.stop()
            ai_terminal.stop_all()
            ui_editor.stop_all()
            benchmark_manager.stop_all()
            arena_manager.stop_all()
            accuracy_manager.stop_all()
            if resolved_settings.worker_enabled:
                worker.stop()
            cluster_monitoring.stop()
            metrics.stop()

    application = FastAPI(
        title="Uniform DGX Spark API",
        version="0.1.0",
        lifespan=lifespan,
    )
    web_dir = Path(__file__).parent / "web"
    application.mount("/static", StaticFiles(directory=web_dir), name="static")

    @application.middleware("http")
    async def api_authentication(request: Request, call_next):  # type: ignore[no-untyped-def]
        configured_token = resolved_settings.api_token
        publication_mutation = request.url.path.startswith(
            "/api/v1/service-publications/"
        ) and request.method in {"POST", "PUT", "PATCH", "DELETE"}
        gateway_mutation = request.url.path.startswith(
            "/api/v1/gateway/"
        ) and request.method in {"POST", "PUT", "PATCH", "DELETE"}
        if publication_mutation or gateway_mutation:
            request_host = (request.url.hostname or "").casefold().rstrip(".")
            if request_host not in publication_control_hosts:
                return JSONResponse(
                    status_code=421,
                    content={"detail": "untrusted control-plane host"},
                )
            action = "service-publication" if publication_mutation else "gateway-config"
            if not hmac.compare_digest(
                request.headers.get("X-Uniform-Action", ""), action
            ):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "control-plane action header is required"},
                )
        capability_action = request.url.path.startswith("/api/v1/ai/sessions/") and (
            "/actions/" in request.url.path
        )
        public_publisher_icon = request.url.path.startswith(
            "/api/v1/catalog/publishers/"
        ) and request.url.path.endswith("/icon")
        if (
            configured_token
            and request.url.path.startswith("/api/")
            and request.url.path != "/api/v1/auth/status"
            and not capability_action
            and not public_publisher_icon
        ):
            supplied = request.headers.get("X-API-Key", "")
            if not hmac.compare_digest(supplied, configured_token):
                return JSONResponse(status_code=401, content={"detail": "invalid API key"})
        response = await call_next(request)
        no_store_api_paths = {
            "/api/v1/catalog/models",
            "/api/v1/catalog/search",
            "/api/v1/catalog/refresh/status",
        }
        if (
            request.url.path == "/"
            or request.url.path.startswith("/static/")
            or request.url.path in no_store_api_paths
        ):
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.get("/", include_in_schema=False)
    def web_index() -> FileResponse:
        return FileResponse(web_dir / "index.html")

    @application.get("/api/v1/auth/status", include_in_schema=False)
    def authentication_status() -> dict[str, bool]:
        return {"required": bool(resolved_settings.api_token)}

    def get_store(request: Request) -> DeploymentStore:
        return request.app.state.store

    def require_ai_enabled() -> None:
        if not resolved_settings.ai_sessions_enabled:
            raise HTTPException(status_code=403, detail="AI sessions are disabled")

    def require_ui_editing_enabled() -> None:
        if not resolved_settings.ui_editing_enabled:
            raise HTTPException(status_code=403, detail="visual UI editing is disabled")

    def require_accuracy_enabled() -> None:
        if not resolved_settings.accuracy_enabled:
            raise HTTPException(status_code=403, detail="accuracy evaluation is disabled")

    def require_accuracy_creation_enabled(_: None = Depends(require_accuracy_enabled)) -> None:
        if not resolved_settings.accuracy_creation_enabled:
            raise HTTPException(status_code=403, detail="accuracy task creation is disabled")

    @application.get("/api/v1/health", response_model=HealthRead)
    def health(request: Request) -> HealthRead:
        database: Database = request.app.state.database
        if not database.check():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="database integrity check failed",
            )
        return HealthRead(
            status="ok",
            database="ok",
            recovered_tasks=request.app.state.recovered_tasks,
        )

    @application.post(
        "/api/v1/deployments",
        response_model=DeploymentRead,
        status_code=status.HTTP_201_CREATED,
    )
    def create_deployment(
        payload: DeploymentCreate,
        store: DeploymentStore = Depends(get_store),
    ) -> DeploymentRead:
        deployment = store.create(payload)
        worker: DeploymentWorker = application.state.worker
        if resolved_settings.worker_enabled:
            worker.wake()
        return deployment

    @application.get("/api/v1/deployments", response_model=DeploymentList)
    def list_deployments(
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        include_archived: bool = Query(default=False),
        store: DeploymentStore = Depends(get_store),
    ) -> DeploymentList:
        items, total = store.list(limit=limit, offset=offset, include_archived=include_archived)
        runtime: DockerRuntime = application.state.runtime
        return DeploymentList(
            items=[_deployment_with_live_status(item, runtime) for item in items],
            total=total,
        )

    @application.get("/api/v1/events/stream", include_in_schema=False)
    async def event_stream(
        request: Request,
        include_archived: bool = Query(default=False),
    ) -> StreamingResponse:
        async def generate() -> AsyncIterator[str]:
            last_payload = ""
            while not await request.is_disconnected():
                store: DeploymentStore = request.app.state.store
                items, total = store.list(limit=200, offset=0, include_archived=include_archived)
                runtime: DockerRuntime = request.app.state.runtime
                payload = json.dumps(
                    {
                        "items": [
                            _deployment_with_live_status(item, runtime).model_dump(mode="json")
                            for item in items
                        ],
                        "total": total,
                    },
                    separators=(",", ":"),
                )
                if payload != last_payload:
                    yield f"event: deployments\ndata: {payload}\n\n"
                    last_payload = payload
                else:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @application.get("/api/v1/deployments/{deployment_id}", response_model=DeploymentRead)
    def get_deployment(
        deployment_id: str,
        store: DeploymentStore = Depends(get_store),
    ) -> DeploymentRead:
        try:
            return _deployment_with_live_status(store.get(deployment_id), application.state.runtime)
        except DeploymentNotFound as error:
            raise HTTPException(status_code=404, detail="deployment not found") from error

    @application.get(
        "/api/v1/deployments/{deployment_id}/events",
        response_model=DeploymentEventList,
    )
    def get_deployment_events(
        deployment_id: str,
        store: DeploymentStore = Depends(get_store),
    ) -> DeploymentEventList:
        try:
            return DeploymentEventList(items=store.list_events(deployment_id))
        except DeploymentNotFound as error:
            raise HTTPException(status_code=404, detail="deployment not found") from error

    @application.post(
        "/api/v1/deployments/{deployment_id}/cancel",
        response_model=DeploymentRead,
    )
    def cancel_deployment(
        deployment_id: str,
        store: DeploymentStore = Depends(get_store),
    ) -> DeploymentRead:
        try:
            worker: DeploymentWorker = application.state.worker
            if resolved_settings.worker_enabled:
                return worker.request_cancel(deployment_id)
            return store.cancel(deployment_id)
        except DeploymentNotFound as error:
            raise HTTPException(status_code=404, detail="deployment not found") from error
        except InvalidTransition as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post(
        "/api/v1/deployments/{deployment_id}/stop",
        response_model=DeploymentRead,
    )
    def stop_deployment(
        deployment_id: str,
        store: DeploymentStore = Depends(get_store),
    ) -> DeploymentRead:
        try:
            if not resolved_settings.worker_enabled:
                raise InvalidTransition("deployment worker is disabled")
            worker: DeploymentWorker = application.state.worker
            stopped = worker.request_stop(deployment_id)
            application.state.publication_manager.revoke("deployment", deployment_id)
            return stopped
        except DeploymentNotFound as error:
            raise HTTPException(status_code=404, detail="deployment not found") from error
        except InvalidTransition as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post(
        "/api/v1/deployments/{deployment_id}/archive",
        response_model=DeploymentRead,
    )
    def archive_deployment(
        deployment_id: str,
        store: DeploymentStore = Depends(get_store),
    ) -> DeploymentRead:
        try:
            deployment = store.get(deployment_id)
            runtime: DockerRuntime = application.state.runtime
            if runtime.is_running(deployment_id):
                raise InvalidTransition("stop the model service before archiving it")
            if deployment.status == DeploymentStatus.RUNNING:
                deployment = store.transition(
                    deployment_id,
                    DeploymentStatus.FAILED,
                    message="Container was no longer running when deployment was archived",
                    error_code="runtime_container_missing",
                    error_message="数据库记录为运行中，但对应容器当前未运行",
                )
            archived = store.archive(deployment.id)
            application.state.publication_manager.revoke("deployment", deployment_id)
            return archived
        except DeploymentNotFound as error:
            raise HTTPException(status_code=404, detail="deployment not found") from error
        except InvalidTransition as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post(
        "/api/v1/deployments/{deployment_id}/restore",
        response_model=DeploymentRead,
    )
    def restore_deployment(
        deployment_id: str,
        store: DeploymentStore = Depends(get_store),
    ) -> DeploymentRead:
        try:
            return store.restore(deployment_id)
        except DeploymentNotFound as error:
            raise HTTPException(status_code=404, detail="deployment not found") from error

    @application.post(
        "/api/v1/deployments/{deployment_id}/retry",
        response_model=DeploymentRead,
    )
    def retry_deployment(
        deployment_id: str,
        store: DeploymentStore = Depends(get_store),
    ) -> DeploymentRead:
        try:
            if not resolved_settings.worker_enabled:
                raise InvalidTransition("deployment worker is disabled")
            worker: DeploymentWorker = application.state.worker
            return worker.request_retry(deployment_id)
        except DeploymentNotFound as error:
            raise HTTPException(status_code=404, detail="deployment not found") from error
        except InvalidTransition as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.get("/api/v1/deployments/{deployment_id}/logs")
    def deployment_logs(
        deployment_id: str,
        tail: int = Query(default=200, ge=1, le=5000),
        store: DeploymentStore = Depends(get_store),
    ) -> dict[str, str]:
        try:
            store.get(deployment_id)
            runtime: DockerRuntime = application.state.runtime
            return {"logs": runtime.logs(deployment_id, tail=tail)}
        except DeploymentNotFound as error:
            raise HTTPException(status_code=404, detail="deployment not found") from error
        except DockerCommandError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.get("/api/v1/system/metrics")
    def system_metrics(
        request: Request,
        history: int = Query(default=0, ge=0, le=300),
    ) -> dict[str, object]:
        metrics: MetricsService = request.app.state.metrics
        return metrics.snapshot(history=history)

    @application.post(
        "/api/v1/benchmarks",
        response_model=BenchmarkRead,
        status_code=status.HTTP_201_CREATED,
    )
    def create_benchmark(payload: BenchmarkCreate, request: Request) -> BenchmarkRead:
        manager: BenchmarkManager = request.app.state.benchmark_manager
        try:
            return manager.create(payload)
        except (DeploymentNotFound, AISessionNotFound, AIDeploymentResultNotFound) as error:
            raise HTTPException(status_code=404, detail="benchmark target not found") from error
        except (BenchmarkTargetError, BenchmarkBusy) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.get("/api/v1/benchmarks", response_model=BenchmarkList)
    def list_benchmarks(
        request: Request,
        limit: int = Query(default=100, ge=1, le=200),
    ) -> BenchmarkList:
        store: BenchmarkStore = request.app.state.benchmark_store
        items, total = store.list(limit=limit)
        return BenchmarkList(items=items, total=total)

    @application.get("/api/v1/benchmarks/{benchmark_id}", response_model=BenchmarkRead)
    def get_benchmark(benchmark_id: str, request: Request) -> BenchmarkRead:
        store: BenchmarkStore = request.app.state.benchmark_store
        try:
            return store.get(benchmark_id)
        except BenchmarkNotFound as error:
            raise HTTPException(status_code=404, detail="benchmark not found") from error

    @application.post(
        "/api/v1/benchmarks/{benchmark_id}/cancel",
        response_model=BenchmarkRead,
    )
    def cancel_benchmark(benchmark_id: str, request: Request) -> BenchmarkRead:
        manager: BenchmarkManager = request.app.state.benchmark_manager
        try:
            return manager.cancel(benchmark_id)
        except BenchmarkNotFound as error:
            raise HTTPException(status_code=404, detail="benchmark not found") from error

    @application.post(
        "/api/v1/accuracy/runs",
        response_model=AccuracyRunRead,
        status_code=status.HTTP_201_CREATED,
    )
    def create_accuracy_run(
        payload: AccuracyRunCreate,
        request: Request,
        _: None = Depends(require_accuracy_creation_enabled),
    ) -> AccuracyRunRead:
        manager: AccuracyManager = request.app.state.accuracy_manager
        try:
            return manager.create(payload)
        except (DeploymentNotFound, AISessionNotFound, AIDeploymentResultNotFound) as error:
            raise HTTPException(status_code=404, detail="accuracy target not found") from error
        except (AccuracyTargetError, AccuracyBusy, AccuracyCompatibilityError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except AccuracyRunNotFound as error:
            raise HTTPException(
                status_code=404, detail="accuracy cache source not found"
            ) from error
        except EvalScopeAccuracyError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except OSError as error:
            raise HTTPException(
                status_code=503, detail="accuracy run storage is unavailable"
            ) from error

    @application.post("/api/v1/accuracy/compare", response_model=AccuracyComparisonRead)
    def compare_accuracy_runs(
        payload: AccuracyCompareCreate,
        request: Request,
        _: None = Depends(require_accuracy_enabled),
    ) -> AccuracyComparisonRead:
        try:
            return request.app.state.accuracy_manager.compare(payload.run_ids)
        except AccuracyRunNotFound as error:
            raise HTTPException(status_code=404, detail="accuracy run not found") from error
        except AccuracyCompatibilityError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post(
        "/api/v1/accuracy/runs/{run_id}/resume", response_model=AccuracyRunRead, status_code=201
    )
    def resume_accuracy_run(
        run_id: str,
        request: Request,
        _: None = Depends(require_accuracy_creation_enabled),
    ) -> AccuracyRunRead:
        try:
            return request.app.state.accuracy_manager.resume(run_id)
        except AccuracyRunNotFound as error:
            raise HTTPException(status_code=404, detail="accuracy run not found") from error
        except (AccuracyTargetError, AccuracyBusy, AccuracyCompatibilityError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (DeploymentNotFound, AISessionNotFound, AIDeploymentResultNotFound) as error:
            raise HTTPException(status_code=404, detail="accuracy target not found") from error
        except EvalScopeAccuracyError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except OSError as error:
            raise HTTPException(status_code=503, detail="accuracy cache unavailable") from error

    @application.get("/api/v1/accuracy/runs", response_model=AccuracyRunList)
    def list_accuracy_runs(
        request: Request,
        limit: int = Query(default=100, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        _: None = Depends(require_accuracy_enabled),
    ) -> AccuracyRunList:
        accuracy_store: AccuracyRunStore = request.app.state.accuracy_store
        items, total = accuracy_store.list(limit=limit, offset=offset)
        return AccuracyRunList(
            items=items, total=total, creation_enabled=resolved_settings.accuracy_creation_enabled
        )

    @application.get("/api/v1/accuracy/runs/{run_id}", response_model=AccuracyRunRead)
    def get_accuracy_run(
        run_id: str,
        request: Request,
        _: None = Depends(require_accuracy_enabled),
    ) -> AccuracyRunRead:
        accuracy_store: AccuracyRunStore = request.app.state.accuracy_store
        try:
            return accuracy_store.get(run_id)
        except AccuracyRunNotFound as error:
            raise HTTPException(status_code=404, detail="accuracy run not found") from error

    @application.post(
        "/api/v1/accuracy/runs/{run_id}/cancel",
        response_model=AccuracyRunRead,
    )
    def cancel_accuracy_run(
        run_id: str,
        request: Request,
        _: None = Depends(require_accuracy_enabled),
    ) -> AccuracyRunRead:
        manager: AccuracyManager = request.app.state.accuracy_manager
        try:
            return manager.cancel(run_id)
        except AccuracyRunNotFound as error:
            raise HTTPException(status_code=404, detail="accuracy run not found") from error

    @application.delete(
        "/api/v1/accuracy/runs/{run_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def delete_accuracy_run(
        run_id: str,
        request: Request,
        _: None = Depends(require_accuracy_enabled),
    ) -> Response:
        manager: AccuracyManager = request.app.state.accuracy_manager
        try:
            manager.delete(run_id)
        except AccuracyRunNotFound as error:
            raise HTTPException(status_code=404, detail="accuracy run not found") from error
        except AccuracyBusy as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @application.get(
        "/api/v1/accuracy/runs/{run_id}/logs",
        response_model=AccuracyLogRead,
    )
    def accuracy_run_logs(
        run_id: str,
        request: Request,
        tail: int = Query(default=500, ge=1, le=5000),
        _: None = Depends(require_accuracy_enabled),
    ) -> AccuracyLogRead:
        manager: AccuracyManager = request.app.state.accuracy_manager
        try:
            return manager.logs(run_id, tail=tail)
        except AccuracyRunNotFound as error:
            raise HTTPException(status_code=404, detail="accuracy run not found") from error

    @application.get(
        "/api/v1/accuracy/runs/{run_id}/artifacts",
        response_model=AccuracyArtifactList,
    )
    def accuracy_run_artifacts(
        run_id: str,
        request: Request,
        _: None = Depends(require_accuracy_enabled),
    ) -> AccuracyArtifactList:
        manager: AccuracyManager = request.app.state.accuracy_manager
        try:
            return manager.artifacts(run_id)
        except AccuracyRunNotFound as error:
            raise HTTPException(status_code=404, detail="accuracy run not found") from error

    @application.get("/api/v1/accuracy/runs/{run_id}/artifacts/{artifact_path:path}")
    def accuracy_run_artifact(
        run_id: str,
        artifact_path: str,
        request: Request,
        _: None = Depends(require_accuracy_enabled),
    ) -> FileResponse:
        manager: AccuracyManager = request.app.state.accuracy_manager
        try:
            path = manager.artifact_path(run_id, artifact_path)
        except AccuracyRunNotFound as error:
            raise HTTPException(status_code=404, detail="accuracy run not found") from error
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="accuracy artifact not found") from error
        return FileResponse(
            path,
            media_type="application/json",
            filename=f"accuracy-{run_id}-{path.name}",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @application.post(
        "/api/v1/arena/runs",
        response_model=ArenaRunRead,
        status_code=status.HTTP_201_CREATED,
    )
    def create_arena_run(payload: ArenaRunCreate, request: Request) -> ArenaRunRead:
        manager: ArenaManager = request.app.state.arena_manager
        try:
            return manager.create(payload)
        except (DeploymentNotFound, AISessionNotFound, AIDeploymentResultNotFound) as error:
            raise HTTPException(status_code=404, detail="Arena target not found") from error
        except (ArenaTargetError, ArenaBusy) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.get("/api/v1/arena/runs", response_model=ArenaRunList)
    def list_arena_runs(
        request: Request,
        limit: int = Query(default=100, ge=1, le=200),
    ) -> ArenaRunList:
        arena_store: ArenaRunStore = request.app.state.arena_store
        items, total = arena_store.list(limit=limit)
        return ArenaRunList(items=items, total=total)

    @application.get("/api/v1/arena/runs/{run_id}", response_model=ArenaRunRead)
    def get_arena_run(run_id: str, request: Request) -> ArenaRunRead:
        arena_store: ArenaRunStore = request.app.state.arena_store
        try:
            return arena_store.get(run_id)
        except ArenaRunNotFound as error:
            raise HTTPException(status_code=404, detail="Arena run not found") from error

    @application.post(
        "/api/v1/arena/runs/{run_id}/cancel",
        response_model=ArenaRunRead,
    )
    def cancel_arena_run(run_id: str, request: Request) -> ArenaRunRead:
        manager: ArenaManager = request.app.state.arena_manager
        try:
            return manager.cancel(run_id)
        except ArenaRunNotFound as error:
            raise HTTPException(status_code=404, detail="Arena run not found") from error

    @application.delete(
        "/api/v1/arena/runs/{run_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def delete_arena_run(run_id: str, request: Request) -> Response:
        manager: ArenaManager = request.app.state.arena_manager
        try:
            manager.delete(run_id)
        except ArenaRunNotFound as error:
            raise HTTPException(status_code=404, detail="Arena run not found") from error
        except ArenaBusy as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @application.get("/api/v1/arena/runs/{run_id}/logs")
    def arena_run_logs(
        run_id: str,
        request: Request,
        tail: int = Query(default=500, ge=1, le=5000),
    ) -> dict[str, str]:
        manager: ArenaManager = request.app.state.arena_manager
        try:
            return {"logs": manager.logs(run_id, tail=tail)}
        except ArenaRunNotFound as error:
            raise HTTPException(status_code=404, detail="Arena run not found") from error

    @application.get("/api/v1/arena/runs/{run_id}/result")
    def arena_run_result(run_id: str, request: Request) -> FileResponse:
        manager: ArenaManager = request.app.state.arena_manager
        try:
            path = manager.result_path(run_id)
        except ArenaRunNotFound as error:
            raise HTTPException(status_code=404, detail="Arena run not found") from error
        except FileNotFoundError as error:
            raise HTTPException(status_code=409, detail="Arena result is not available") from error
        return FileResponse(path, media_type="application/json", filename=f"arena-{run_id}.json")

    @application.get("/api/v1/arena/runs/{run_id}/report")
    def arena_run_report(run_id: str, request: Request) -> JSONResponse:
        manager: ArenaManager = request.app.state.arena_manager
        try:
            report = manager.report(run_id)
        except ArenaRunNotFound as error:
            raise HTTPException(status_code=404, detail="Arena run not found") from error
        return JSONResponse(
            report,
            headers={"Content-Disposition": f'attachment; filename="arena-{run_id}-report.json"'},
        )

    @application.get("/api/v1/arena/leaderboard", response_model=ArenaLeaderboardList)
    def arena_leaderboard(
        request: Request,
        query: str = Query(default="", max_length=255),
        limit: int = Query(default=100, ge=1, le=240),
    ) -> ArenaLeaderboardList:
        manager: ArenaManager = request.app.state.arena_manager
        try:
            return manager.leaderboard(query=query, limit=limit)
        except ArenaLeaderboardError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    @application.get(
        "/api/v1/arena/leaderboard/{benchmark_id}",
        response_model=ArenaLeaderboardDetail,
    )
    def arena_leaderboard_detail(
        benchmark_id: str,
        request: Request,
    ) -> ArenaLeaderboardDetail:
        manager: ArenaManager = request.app.state.arena_manager
        try:
            return manager.leaderboard_detail(benchmark_id)
        except ArenaLeaderboardError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    @application.get(
        "/api/v1/arena/runtime-profile",
        response_model=ArenaRuntimeProfile,
    )
    def arena_runtime_profile(
        request: Request,
        target_type: str = Query(pattern=r"^(deployment|ai_session)$"),
        target_id: str = Query(min_length=1, max_length=255),
    ) -> ArenaRuntimeProfile:
        manager: ArenaManager = request.app.state.arena_manager
        try:
            return manager.runtime_profile(target_type, target_id)
        except (DeploymentNotFound, AISessionNotFound, AIDeploymentResultNotFound) as error:
            raise HTTPException(status_code=404, detail="Arena target not found") from error
        except ArenaTargetError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.get("/api/v1/system/nodes")
    def system_nodes(request: Request) -> dict[str, object]:
        monitoring: ClusterMonitoringService = request.app.state.cluster_monitoring
        return monitoring.snapshot()

    @application.post(
        "/api/v1/system/nodes",
        response_model=SSHNodeRead,
        status_code=status.HTTP_201_CREATED,
    )
    def add_system_node(payload: SSHNodeCreate, request: Request) -> SSHNodeRead:
        store: SSHNodeStore = request.app.state.ssh_node_store
        try:
            node = store.create(payload)
        except SSHNodeAlreadyExists as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        request.app.state.cluster_monitoring.wake()
        return node

    @application.delete("/api/v1/system/nodes/{node_id}")
    def delete_system_node(node_id: str, request: Request) -> dict[str, bool]:
        store: SSHNodeStore = request.app.state.ssh_node_store
        removed = store.delete(node_id)
        request.app.state.cluster_monitoring.wake()
        return {"removed": removed}

    @application.post("/api/v1/system/nodes/refresh")
    def refresh_system_nodes(request: Request) -> dict[str, object]:
        monitoring: ClusterMonitoringService = request.app.state.cluster_monitoring
        return monitoring.refresh(wait=True)

    @application.get("/api/v1/images", response_model=LocalImageList)
    def local_images(request: Request) -> LocalImageList:
        runtime: DockerRuntime = request.app.state.runtime
        try:
            items = runtime.list_images()
        except DockerCommandError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return LocalImageList(items=items, total=len(items))

    @application.get("/api/v1/local-models", response_model=LocalModelInventory)
    def local_models(request: Request) -> LocalModelInventory:
        service: LocalModelService = request.app.state.local_models
        return service.scan()

    @application.post(
        "/api/v1/local-models/roots",
        response_model=LocalModelRootRead,
        status_code=status.HTTP_201_CREATED,
    )
    def add_local_model_root(
        payload: LocalModelRootCreate,
        request: Request,
    ) -> LocalModelRootRead:
        service: LocalModelService = request.app.state.local_models
        try:
            return service.add_root(payload.path)
        except LocalModelRootError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @application.delete("/api/v1/local-models/roots")
    def remove_local_model_root(
        request: Request,
        path: str = Query(min_length=1, max_length=4096),
    ) -> dict[str, bool]:
        service: LocalModelService = request.app.state.local_models
        return {"removed": service.remove_root(path)}

    @application.get("/api/v1/ui-editor/status", response_model=UIEditorStatus)
    def ui_editor_status(request: Request) -> UIEditorStatus:
        manager: UIEditManager = request.app.state.ui_editor
        return manager.status()

    @application.post("/api/v1/ui-editor/capture", response_class=Response)
    def capture_ui_page(
        payload: UIPageCaptureCreate,
        request: Request,
        _: None = Depends(require_ui_editing_enabled),
    ) -> Response:
        service: UIPageCaptureService = request.app.state.ui_page_capture
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        base_url = f"{request.url.scheme}://127.0.0.1:{port}"
        try:
            image = service.capture(payload, base_url=base_url)
        except UIPageCaptureUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except UIPageCaptureError as error:
            raise HTTPException(status_code=500, detail=str(error)) from error
        return Response(
            content=image,
            media_type="image/png",
            headers={"Cache-Control": "no-store", "X-Uniform-Capture-Engine": "chromium"},
        )

    @application.post(
        "/api/v1/ui-edits",
        response_model=UIEditRead,
        status_code=status.HTTP_201_CREATED,
    )
    def create_ui_edit(
        payload: UIEditCreate,
        request: Request,
        _: None = Depends(require_ui_editing_enabled),
    ) -> UIEditRead:
        manager: UIEditManager = request.app.state.ui_editor
        try:
            return manager.submit(payload)
        except UIEditBusy as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except UIEditorUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @application.get("/api/v1/ui-edits", response_model=UIEditList)
    def list_ui_edits(
        request: Request,
        limit: int = Query(default=20, ge=1, le=100),
    ) -> UIEditList:
        manager: UIEditManager = request.app.state.ui_editor
        items, total = manager.list(limit=limit)
        return UIEditList(items=items, total=total)

    @application.get("/api/v1/ui-edits/{job_id}", response_model=UIEditRead)
    def get_ui_edit(job_id: str, request: Request) -> UIEditRead:
        manager: UIEditManager = request.app.state.ui_editor
        try:
            return manager.get(job_id)
        except UIEditNotFound as error:
            raise HTTPException(status_code=404, detail="UI edit not found") from error

    @application.get("/api/v1/ui-edits/{job_id}/logs", response_model=UIEditLogRead)
    def get_ui_edit_logs(
        job_id: str,
        request: Request,
        tail_bytes: int = Query(default=50_000, ge=1_000, le=1_000_000),
    ) -> UIEditLogRead:
        manager: UIEditManager = request.app.state.ui_editor
        try:
            return manager.logs(job_id, tail_bytes=tail_bytes)
        except UIEditNotFound as error:
            raise HTTPException(status_code=404, detail="UI edit not found") from error

    @application.get(
        "/api/v1/ui-edits/{job_id}/conversation",
        response_model=UIEditConversationRead,
    )
    def get_ui_edit_conversation(
        job_id: str,
        request: Request,
        limit: int = Query(default=300, ge=1, le=1_000),
    ) -> UIEditConversationRead:
        manager: UIEditManager = request.app.state.ui_editor
        try:
            return manager.conversation(job_id, limit=limit)
        except UIEditNotFound as error:
            raise HTTPException(status_code=404, detail="UI edit not found") from error

    @application.post("/api/v1/ui-edits/{job_id}/messages", response_model=UIEditRead)
    def intervene_ui_edit(
        job_id: str,
        payload: UIEditMessageCreate,
        request: Request,
        _: None = Depends(require_ui_editing_enabled),
    ) -> UIEditRead:
        manager: UIEditManager = request.app.state.ui_editor
        try:
            return manager.intervene(job_id, payload.message)
        except UIEditNotFound as error:
            raise HTTPException(status_code=404, detail="UI edit not found") from error
        except UIEditConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post("/api/v1/ui-edits/{job_id}/stop", response_model=UIEditRead)
    def stop_ui_edit(
        job_id: str,
        request: Request,
        _: None = Depends(require_ui_editing_enabled),
    ) -> UIEditRead:
        manager: UIEditManager = request.app.state.ui_editor
        try:
            return manager.stop(job_id)
        except UIEditNotFound as error:
            raise HTTPException(status_code=404, detail="UI edit not found") from error
        except UIEditConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post("/api/v1/ui-edits/{job_id}/retry", response_model=UIEditRead)
    def retry_ui_edit(
        job_id: str,
        request: Request,
        _: None = Depends(require_ui_editing_enabled),
    ) -> UIEditRead:
        manager: UIEditManager = request.app.state.ui_editor
        try:
            return manager.retry(job_id)
        except UIEditNotFound as error:
            raise HTTPException(status_code=404, detail="UI edit not found") from error
        except (UIEditBusy, UIEditConflict) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post("/api/v1/ui-edits/{job_id}/revert", response_model=UIEditRead)
    def revert_ui_edit(
        job_id: str,
        payload: UIEditRevertCreate,
        request: Request,
        _: None = Depends(require_ui_editing_enabled),
    ) -> UIEditRead:
        manager: UIEditManager = request.app.state.ui_editor
        try:
            return manager.revert(job_id, payload.reason)
        except UIEditNotFound as error:
            raise HTTPException(status_code=404, detail="UI edit not found") from error
        except (UIEditBusy, UIEditConflict) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.get("/api/v1/ai/providers", response_model=list[AIProviderRead])
    def ai_providers(_: None = Depends(require_ai_enabled)) -> list[AIProviderRead]:
        manager: AITerminalManager = application.state.ai_terminal
        return manager.providers()

    @application.post(
        "/api/v1/ai/sessions",
        response_model=AISessionRead,
        status_code=status.HTTP_201_CREATED,
    )
    def create_ai_session(
        payload: AISessionCreate,
        _: None = Depends(require_ai_enabled),
    ) -> AISessionRead:
        if payload.client_request_id:
            try:
                return application.state.ai_store.get_by_client_request_id(
                    payload.client_request_id
                )
            except AISessionNotFound:
                pass
        deployment_context: dict[str, object] | None = None
        deployment_logs: str | None = None
        if payload.task_type == "accuracy_evaluation":
            require_accuracy_enabled()
            require_accuracy_creation_enabled()
            try:
                target = application.state.accuracy_manager.target_resolver.resolve(
                    payload.accuracy_target_type, payload.accuracy_target_id
                )
            except (DeploymentNotFound, AISessionNotFound, AIDeploymentResultNotFound) as error:
                raise HTTPException(status_code=404, detail="accuracy target not found") from error
            except ModelTargetError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            deployment_context = {
                "accuracy_target": {
                    "target_type": payload.accuracy_target_type,
                    "target_id": payload.accuracy_target_id,
                    "label": target.label,
                    "model_name": target.model_name,
                },
                "sample_budget": payload.accuracy_sample_budget,
            }
        elif payload.task_type == "dataset_integration":
            require_accuracy_enabled()
            require_accuracy_creation_enabled()
            if payload.accuracy_dataset_name not in application.state.dataset_integrations.items:
                raise HTTPException(status_code=422, detail="dataset is not in the pinned catalog")
            deployment_context = {"dataset_name": payload.accuracy_dataset_name}
            if payload.accuracy_target_id:
                try:
                    target = application.state.accuracy_manager.target_resolver.resolve(
                        payload.accuracy_target_type, payload.accuracy_target_id
                    )
                except LookupError as error:
                    raise HTTPException(
                        status_code=404, detail="integration target not found"
                    ) from error
                except ModelTargetError as error:
                    raise HTTPException(status_code=409, detail=str(error)) from error
                deployment_context["accuracy_target"] = {
                    "target_type": payload.accuracy_target_type,
                    "target_id": payload.accuracy_target_id,
                    "label": target.label,
                    "model_name": target.model_name,
                }
        elif payload.task_type == "benchmark_execution":
            target_type = payload.benchmark_target_type
            target_id = payload.benchmark_target_id
            runtime: DockerRuntime = application.state.runtime
            metrics: MetricsService = application.state.metrics
            cluster: ClusterMonitoringService = application.state.cluster_monitoring
            service: dict[str, object]
            if target_type == "deployment" and target_id:
                store: DeploymentStore = application.state.store
                try:
                    deployment = store.get(target_id)
                except DeploymentNotFound as error:
                    raise HTTPException(
                        status_code=404, detail="benchmark target not found"
                    ) from error
                if deployment.status.value != "running":
                    raise HTTPException(status_code=409, detail="benchmark target is not running")
                container: dict[str, object] | None = None
                try:
                    container = runtime.container_metadata(deployment)
                    deployment_logs = runtime.logs(deployment.id, tail=1000)
                except DockerCommandError:
                    deployment_logs = None
                port = deployment.parameters.port
                service = {
                    "target_type": target_type,
                    "target_id": target_id,
                    "label": deployment.display_name or deployment.model_id,
                    "model_name": deployment.parameters.served_model_name or deployment.model_id,
                    "service_url": f"http://127.0.0.1:{port}",
                    "port": port,
                    "status": deployment.status.value,
                    "deployment": deployment.model_dump(mode="json"),
                    "container": container,
                    "inference_telemetry": metrics.inference_telemetry(port),
                }
            elif target_type == "ai_session" and target_id:
                ai_store: AISessionStore = application.state.ai_store
                try:
                    source_session = ai_store.get(target_id)
                    result = ai_store.get_result(target_id)
                except (AISessionNotFound, AIDeploymentResultNotFound) as error:
                    raise HTTPException(
                        status_code=404, detail="benchmark target not found"
                    ) from error
                if result.status != "verified":
                    raise HTTPException(status_code=409, detail="benchmark target is not verified")
                linked_deployment: dict[str, object] | None = None
                if source_session.deployment_id:
                    try:
                        linked_deployment = application.state.store.get(
                            source_session.deployment_id
                        ).model_dump(mode="json")
                    except DeploymentNotFound:
                        pass
                try:
                    deployment_logs = runtime.logs_for_container(result.container_name, tail=1000)
                except DockerCommandError:
                    deployment_logs = None
                service = {
                    "target_type": target_type,
                    "target_id": target_id,
                    "label": source_session.display_name,
                    "model_name": source_session.target_model_id or source_session.display_name,
                    "service_url": result.service_url,
                    "port": result.port,
                    "status": result.status,
                    "source_session": source_session.model_dump(mode="json"),
                    "deployment_result": result.model_dump(mode="json"),
                    "linked_deployment": linked_deployment,
                    "inference_telemetry": metrics.inference_telemetry(result.port),
                }
            else:
                raise HTTPException(status_code=422, detail="invalid benchmark target")
            try:
                runtime_profile = application.state.arena_manager.runtime_profile(
                    target_type, target_id
                )
                service["runtime_profile"] = runtime_profile.model_dump(mode="json")
            except Exception:  # noqa: BLE001 - optional live deployment metadata
                service["runtime_profile"] = None
            deployment_context = {
                "captured_at": datetime.now().astimezone().isoformat(),
                "service": service,
                "host_metrics": metrics.snapshot(),
                "cluster": cluster.snapshot(),
                "benchmark_plan": (
                    payload.benchmark_plan.model_dump(mode="json")
                    if payload.benchmark_plan is not None
                    else None
                ),
            }
        elif payload.deployment_id:
            store: DeploymentStore = application.state.store
            try:
                deployment = store.get(payload.deployment_id)
            except DeploymentNotFound as error:
                raise HTTPException(status_code=404, detail="deployment not found") from error
            deployment_context = deployment.model_dump(mode="json")
            runtime: DockerRuntime = application.state.runtime
            try:
                deployment_logs = runtime.logs(payload.deployment_id, tail=1000)
            except DockerCommandError:
                deployment_logs = None
        elif payload.target_model_id and payload.target_local_path:
            local_model_service: LocalModelService = application.state.local_models
            try:
                local_model = local_model_service.get_registered_model(payload.target_local_path)
            except (LocalModelRootError, OSError) as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            runtime: DockerRuntime = application.state.runtime
            try:
                local_images = [image.model_dump(mode="json") for image in runtime.list_images()]
            except DockerCommandError:
                local_images = []
            deployment_context = {
                "target_model": {
                    "model_id": payload.target_model_id,
                    "source": "local",
                    "revision": None,
                    "local_model_path": local_model.path,
                    "catalog": local_model.model_dump(mode="json"),
                },
                "available_local_images": local_images,
            }
        elif payload.target_model_id and payload.target_source:
            catalog_store: CatalogStore = application.state.catalog_store
            catalog_items, _ = catalog_store.search(
                query=payload.target_model_id,
                source=payload.target_source,
                limit=20,
            )
            catalog_item = next(
                (item for item in catalog_items if item.model_id == payload.target_model_id),
                None,
            )
            runtime: DockerRuntime = application.state.runtime
            try:
                local_images = [image.model_dump(mode="json") for image in runtime.list_images()]
            except DockerCommandError:
                local_images = []
            deployment_context = {
                "target_model": {
                    "model_id": payload.target_model_id,
                    "source": payload.target_source,
                    "revision": payload.target_revision,
                    "catalog": (catalog_item.model_dump(mode="json") if catalog_item else None),
                },
                "available_local_images": local_images,
            }
        manager: AITerminalManager = application.state.ai_terminal
        try:
            return manager.create(
                payload,
                deployment_context=deployment_context,
                deployment_logs=deployment_logs,
            )
        except AIProviderUnavailable as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    @application.get("/api/v1/ai/sessions", response_model=AISessionList)
    def list_ai_sessions(
        limit: int = Query(default=100, ge=1, le=200),
        include_archived: bool = Query(default=False),
        target_model_id: str | None = Query(default=None, min_length=1, max_length=255),
        task_type: str | None = Query(
            default=None,
            pattern=(
                r"^(deployment|diagnosis|benchmark_execution|benchmark_analysis|"
                r"accuracy_evaluation|dataset_integration|service_stop|general)$"
            ),
        ),
        _: None = Depends(require_ai_enabled),
    ) -> AISessionList:
        store: AISessionStore = application.state.ai_store
        items, total = store.list(
            limit=limit,
            include_archived=include_archived,
            target_model_id=target_model_id,
            task_type=task_type,
        )
        return AISessionList(items=items, total=total)

    @application.get("/api/v1/ai/sessions/{session_id}", response_model=AISessionRead)
    def get_ai_session(
        session_id: str,
        _: None = Depends(require_ai_enabled),
    ) -> AISessionRead:
        store: AISessionStore = application.state.ai_store
        try:
            return store.get(session_id)
        except AISessionNotFound as error:
            raise HTTPException(status_code=404, detail="AI session not found") from error

    @application.get(
        "/api/v1/ai/sessions/{session_id}/conversation",
        response_model=AIConversationRead,
    )
    def get_ai_conversation(
        session_id: str,
        _: None = Depends(require_ai_enabled),
    ) -> AIConversationRead:
        manager: AITerminalManager = application.state.ai_terminal
        try:
            return manager.conversation(session_id)
        except AISessionNotFound as error:
            raise HTTPException(status_code=404, detail="AI session not found") from error

    @application.post(
        "/api/v1/ai/sessions/{session_id}/conversation/messages",
        response_model=AISessionRead,
    )
    def send_ai_conversation_message(
        session_id: str,
        payload: AIConversationMessageCreate,
        _: None = Depends(require_ai_enabled),
    ) -> AISessionRead:
        manager: AITerminalManager = application.state.ai_terminal
        try:
            return manager.send_instruction(session_id, payload.text)
        except AISessionNotFound as error:
            raise HTTPException(status_code=404, detail="AI session not found") from error
        except AISessionBusy as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except AIProviderUnavailable as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    @application.get(
        "/api/v1/ai/sessions/{session_id}/performance-report",
        response_model=None,
    )
    def ai_performance_report_status(
        session_id: str,
        download: bool = Query(default=False),
        _: None = Depends(require_ai_enabled),
    ) -> dict[str, object] | FileResponse:
        store: AISessionStore = application.state.ai_store
        try:
            session = store.get(session_id)
        except AISessionNotFound as error:
            raise HTTPException(status_code=404, detail="AI session not found") from error
        if session.task_type not in {"benchmark_analysis", "benchmark_execution"}:
            raise HTTPException(status_code=409, detail="session is not a benchmark session")
        internal_filename = "MODEL_PERFORMANCE_REPORT.md"
        download_filename = _performance_report_download_filename(session)
        report_path = Path(session.workspace_path) / internal_filename
        available = report_path.is_file() and not report_path.is_symlink()
        if download:
            if not available:
                raise HTTPException(
                    status_code=409,
                    detail="AI has not generated the performance report yet",
                )
            return FileResponse(
                report_path,
                media_type="text/markdown; charset=utf-8",
                filename=download_filename,
            )
        return {
            "available": available,
            "filename": download_filename,
            "path": str(report_path),
            "size_bytes": report_path.stat().st_size if available else 0,
            "updated_at": (
                datetime.fromtimestamp(report_path.stat().st_mtime).astimezone().isoformat()
                if available
                else None
            ),
        }

    @application.get("/api/v1/ai/sessions/{session_id}/benchmark-result")
    def ai_benchmark_result(
        session_id: str,
        publish: bool = Query(default=True),
        _: None = Depends(require_ai_enabled),
    ) -> dict[str, object]:
        try:
            return application.state.ai_terminal.benchmark_result(
                session_id,
                publish=publish,
            )
        except AISessionNotFound as error:
            raise HTTPException(status_code=404, detail="AI session not found") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post(
        "/api/v1/ai/sessions/{session_id}/resume",
        response_model=AISessionRead,
    )
    def resume_ai_session(
        session_id: str,
        payload: AISessionResume,
        _: None = Depends(require_ai_enabled),
    ) -> AISessionRead:
        manager: AITerminalManager = application.state.ai_terminal
        try:
            return manager.resume(session_id, prompt=payload.prompt)
        except AISessionNotFound as error:
            raise HTTPException(status_code=404, detail="AI session not found") from error
        except AIProviderUnavailable as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except AISessionBusy as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    @application.post(
        "/api/v1/ai/sessions/{session_id}/archive",
        response_model=AISessionRead,
    )
    def archive_ai_session(
        session_id: str,
        _: None = Depends(require_ai_enabled),
    ) -> AISessionRead:
        manager: AITerminalManager = application.state.ai_terminal
        try:
            session = application.state.ai_store.get(session_id)
            if application.state.ai_accuracy.is_active(session_id):
                raise AISessionBusy("cancel the AI accuracy workflow before archiving its session")
            if application.state.dataset_integrations.is_active(session_id):
                raise AISessionBusy("cancel dataset validation before archiving its session")
            if session.task_type == "deployment":
                try:
                    result = application.state.ai_store.get_result(session_id)
                except AIDeploymentResultNotFound:
                    result = None
                if result is not None and application.state.runtime.is_named_container_running(
                    result.container_name
                ):
                    raise AISessionBusy("stop the model service before archiving its session")
            return manager.archive(session_id)
        except AISessionNotFound as error:
            raise HTTPException(status_code=404, detail="AI session not found") from error
        except AISessionBusy as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post(
        "/api/v1/ai/sessions/{session_id}/restore",
        response_model=AISessionRead,
    )
    def restore_ai_session(
        session_id: str,
        _: None = Depends(require_ai_enabled),
    ) -> AISessionRead:
        manager: AITerminalManager = application.state.ai_terminal
        try:
            return manager.restore(session_id)
        except AISessionNotFound as error:
            raise HTTPException(status_code=404, detail="AI session not found") from error

    @application.delete(
        "/api/v1/ai/sessions/{session_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def delete_ai_session(
        session_id: str,
        force: bool = Query(default=False),
        _: None = Depends(require_ai_enabled),
    ) -> Response:
        manager: AITerminalManager = application.state.ai_terminal
        if application.state.ai_accuracy.is_active(session_id):
            raise HTTPException(
                status_code=409, detail="cancel the AI accuracy workflow before deletion"
            )
        if application.state.dataset_integrations.is_active(session_id):
            raise HTTPException(status_code=409, detail="cancel dataset validation before deletion")
        try:
            try:
                application.state.publication_manager.revoke("ai_session", session_id)
            except PublicationTargetNotFound:
                pass
            manager.delete(session_id, force=force)
            application.state.publication_manager.revoke("ai_session", session_id)
        except AISessionNotFound as error:
            raise HTTPException(status_code=404, detail="AI session not found") from error
        except AISessionBusy as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @application.get("/api/v1/ai/sessions/{session_id}/audit", response_model=AIAuditEventList)
    def ai_session_audit(
        session_id: str,
        _: None = Depends(require_ai_enabled),
    ) -> AIAuditEventList:
        store: AISessionStore = application.state.ai_store
        try:
            return AIAuditEventList(items=store.list_audit(session_id))
        except AISessionNotFound as error:
            raise HTTPException(status_code=404, detail="AI session not found") from error

    @application.get(
        "/api/v1/ai/sessions/{session_id}/result",
        response_model=AIDeploymentResultRead,
    )
    def ai_session_result(
        session_id: str,
        _: None = Depends(require_ai_enabled),
    ) -> AIDeploymentResultRead:
        store: AISessionStore = application.state.ai_store
        try:
            return store.get_result(session_id)
        except AISessionNotFound as error:
            raise HTTPException(status_code=404, detail="AI session not found") from error
        except AIDeploymentResultNotFound as error:
            raise HTTPException(status_code=404, detail="deployment result not reported") from error

    @application.get(
        "/api/v1/ai/deployment-templates/latest",
        response_model=AIDeploymentTemplateRead,
    )
    def latest_ai_deployment_template(
        model_id: str = Query(min_length=1, max_length=255),
        target_source: str = Query(pattern=r"^(huggingface|modelscope|local)$"),
        target_revision: str | None = Query(default=None, max_length=255),
        target_local_path: str | None = Query(default=None, max_length=4096),
        deployment_topology: str = Query(
            default="single_node", pattern=r"^(single_node|multi_node)$"
        ),
        _: None = Depends(require_ai_enabled),
    ) -> AIDeploymentTemplateRead:
        try:
            return application.state.ai_store.latest_template(
                model_id=model_id,
                target_source=target_source,
                target_revision=target_revision,
                target_local_path=target_local_path,
                deployment_topology=deployment_topology,
            )
        except AIDeploymentTemplateNotFound as error:
            raise HTTPException(
                status_code=404, detail="no verified reusable deployment template"
            ) from error

    def create_reuse_fallback_session(
        payload: AIDeploymentReuseRequest,
        template: AIDeploymentTemplateRead | None,
        reason: str,
    ) -> AISessionRead:
        manager: AITerminalManager = application.state.ai_terminal
        target: dict[str, object] = {
            "model_id": payload.model_id,
            "source": payload.target_source,
            "revision": payload.target_revision,
            "local_model_path": payload.target_local_path,
            "catalog": None,
        }
        if payload.target_source == "local" and payload.target_local_path:
            try:
                local_model = application.state.local_models.get_registered_model(
                    payload.target_local_path
                )
                target["catalog"] = local_model.model_dump(mode="json")
            except (LocalModelRootError, OSError):
                pass
        elif payload.target_source in {"huggingface", "modelscope"}:
            catalog_items, _ = application.state.catalog_store.search(
                query=payload.model_id,
                source=payload.target_source,
                limit=20,
            )
            catalog_item = next(
                (item for item in catalog_items if item.model_id == payload.model_id),
                None,
            )
            target["catalog"] = catalog_item.model_dump(mode="json") if catalog_item else None
        try:
            local_images = [
                image.model_dump(mode="json") for image in application.state.runtime.list_images()
            ]
        except DockerCommandError:
            local_images = []
        template_context = template.model_dump(mode="json") if template else None
        template_manifest_path = (
            str(Path(template.script_path).with_name("deployment-template.json"))
            if template
            else None
        )
        deployment_context = {
            "target_model": target,
            "available_local_images": local_images,
            "reusable_template": template_context,
            "reusable_template_manifest": template_manifest_path,
            "reuse_failure": reason,
        }
        initial_prompt = (
            "完成该模型部署。平台优先复用最近一次已验证部署模板，但模板无法安全直接"
            f"恢复：{reason}。先阅读 deployment-context.json 中的 reusable_template 和 "
            "reuse_failure，并读取 reusable_template_manifest 指向的 deployment-template.json。"
            "使用其中已脱敏的 launch_command_template 作为原始启动命令参考；不要把 "
            "start-reusable.sh（它只是固定容器的 docker start 包装）误当成原始部署命令。"
            "重新核对当前路径、镜像、端口和运行环境，"
            "再部署、健康检查并执行真实推理测试。成功后按 RESULT_SCHEMA.md 提交结果，"
            "以生成新的可复用模板。"
        )
        request = AISessionCreate(
            provider=payload.provider,
            task_type="deployment",
            client_request_id=(
                f"reuse:{payload.client_request_id}"
                if len(payload.client_request_id) <= 122
                else "reuse:" + hashlib.sha256(payload.client_request_id.encode()).hexdigest()
            ),
            display_name=payload.display_name,
            ai_model=payload.ai_model,
            reasoning_effort=payload.reasoning_effort,
            execution_mode=payload.execution_mode,
            full_access_acknowledged=payload.full_access_acknowledged,
            initial_prompt=initial_prompt,
            reference_urls=template.reference_urls if template else [],
            target_model_id=payload.model_id,
            target_source=(payload.target_source if payload.target_source != "local" else None),
            target_revision=payload.target_revision,
            target_local_path=payload.target_local_path,
            deployment_topology=payload.deployment_topology,
        )
        return manager.create(request, deployment_context=deployment_context)

    @application.post(
        "/api/v1/ai/deployment-templates/launch",
        response_model=AIDeploymentReuseRead,
    )
    def launch_from_ai_deployment_template(
        payload: AIDeploymentReuseRequest,
        _: None = Depends(require_ai_enabled),
    ) -> AIDeploymentReuseRead:
        ai_store: AISessionStore = application.state.ai_store
        manager: AITerminalManager = application.state.ai_terminal
        template_manager: AIDeploymentTemplateManager = application.state.ai_deployment_templates
        runtime: DockerRuntime = application.state.runtime
        fingerprint_payload = payload.model_dump(mode="json", exclude={"client_request_id"})
        request_fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        try:
            existing_response = ai_store.begin_reuse_request(
                payload.client_request_id, request_fingerprint
            )
        except AIDeploymentReuseConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except AIDeploymentReusePending as error:
            raise HTTPException(
                status_code=409,
                detail="the same deployment request is already being processed",
                headers={"Retry-After": "1"},
            ) from error
        if existing_response is not None:
            return existing_response

        def finish(response: AIDeploymentReuseRead) -> AIDeploymentReuseRead:
            ai_store.complete_reuse_request(payload.client_request_id, response)
            return response

        template: AIDeploymentTemplateRead | None = None
        failure_reason = "未找到与模型身份和部署拓扑完全匹配的已验证模板"
        try:
            template = ai_store.latest_template(
                model_id=payload.model_id,
                target_source=payload.target_source,
                target_revision=payload.target_revision,
                target_local_path=payload.target_local_path,
                deployment_topology=payload.deployment_topology,
            )
            template_manager.validate(template)
            if runtime.is_named_container_running(template.container_name):
                running_identity = runtime.named_container_identity(template.container_name)
                if template.container_id is None:
                    raise DeploymentTemplateInvalid(
                        "the running service has no verified container identity"
                    )
                if running_identity is None:
                    raise DeploymentTemplateInvalid(
                        "the running model container identity is unavailable"
                    )
                if running_identity["container_id"] != template.container_id:
                    raise DeploymentTemplateInvalid("the running model container identity changed")
                if running_identity["image"] != template.image:
                    raise DeploymentTemplateInvalid("the running model container image changed")
                if running_identity.get("auto_remove") is True:
                    raise DeploymentTemplateInvalid(
                        "the running model container uses auto-remove and cannot be reused"
                    )
                if not running_identity["running"]:
                    raise DeploymentTemplateInvalid(
                        "the reported running model container is not running"
                    )
                if not runtime.health(template.port, timeout=3.0):
                    # A verified direct artifact may recover an unhealthy bound container below.
                    if template.reuse_mode != "direct_script":
                        raise DeploymentTemplateInvalid("the running model service is not healthy")
                else:
                    served_model = runtime.smoke_test_service(
                        template.port,
                        model_name=template.model_id,
                        endpoint=template.inference_endpoint,
                        timeout=60,
                    )
                    source_session = None
                    if template.source_session_id:
                        try:
                            source_session = ai_store.get(template.source_session_id)
                        except AISessionNotFound:
                            pass
                    return finish(
                        AIDeploymentReuseRead(
                            reuse_method="running_service",
                            fallback_to_ai=False,
                            message=(
                                "已核对容器身份、镜像并通过真实推理，直接复用当前服务"
                                f"（模型 {served_model}）"
                            ),
                            template=template,
                            session=source_session,
                        )
                    )

            if template.reuse_mode == "managed":
                if not runtime.image_exists(template.image):
                    raise DeploymentTemplateInvalid("verified image is no longer local")
                runtime.ensure_port_available(template.port)
                parameters = DeploymentParameters.model_validate(template.parameters)
                if parameters.image != template.image:
                    raise DeploymentTemplateInvalid("managed image binding changed")
                if parameters.port != template.port:
                    raise DeploymentTemplateInvalid("managed port binding changed")
                if template.target_source == "local" and (
                    parameters.local_model_path != template.target_local_path
                ):
                    raise DeploymentTemplateInvalid("local model binding changed")
                if template.target_source in {"huggingface", "modelscope"} and (
                    parameters.source != template.target_source
                ):
                    raise DeploymentTemplateInvalid("remote model source binding changed")
                deployment = application.state.store.create(
                    DeploymentCreate(
                        model_id=template.model_id,
                        revision=template.target_revision,
                        display_name=payload.display_name or f"复用 · {template.model_id}",
                        parameters=parameters,
                    )
                )
                if resolved_settings.worker_enabled:
                    application.state.worker.wake()
                if template.source_session_id:
                    try:
                        ai_store.add_audit(
                            template.source_session_id,
                            "deployment_template_reused",
                            "Created a managed deployment from the verified template",
                            {
                                "template_id": template.id,
                                "deployment_id": deployment.id,
                            },
                        )
                    except AISessionNotFound:
                        pass
                return finish(
                    AIDeploymentReuseRead(
                        reuse_method="managed_deployment",
                        fallback_to_ai=False,
                        message="已按最近一次验证参数创建托管部署",
                        template=template,
                        deployment=deployment,
                    )
                )

            if template.reuse_mode == "direct_script":
                try:
                    with template_manager.operation(template.id):
                        identity = runtime.named_container_identity(template.container_name)
                        if identity is None:
                            raise DeploymentTemplateInvalid(
                                "the verified model container no longer exists"
                            )
                        if identity["container_id"] != template.container_id:
                            raise DeploymentTemplateInvalid(
                                "the verified model container identity changed"
                            )
                        if identity["image"] != template.image:
                            raise DeploymentTemplateInvalid(
                                "the verified model container image changed"
                            )
                        if identity.get("auto_remove") is True:
                            raise DeploymentTemplateInvalid(
                                "the verified model container uses auto-remove"
                            )
                        if identity["running"]:
                            template_manager.execute(template, "stop")
                            stop_deadline = time.monotonic() + 45
                            while time.monotonic() < stop_deadline:
                                if not runtime.is_named_container_running(template.container_name):
                                    break
                                time.sleep(0.5)
                            else:
                                raise DeploymentTemplateInvalid(
                                    "the unhealthy model container could not be stopped"
                                )
                        template_manager.execute(template, "start")
                        ready_deadline = (
                            time.monotonic() + resolved_settings.default_startup_timeout_seconds
                        )
                        while time.monotonic() < ready_deadline:
                            current_identity = runtime.named_container_identity(
                                template.container_name
                            )
                            if (
                                current_identity is not None
                                and current_identity.get("auto_remove") is True
                            ):
                                raise DeploymentTemplateInvalid(
                                    "the restarted model container uses auto-remove"
                                )
                            if (
                                current_identity is not None
                                and current_identity["container_id"] == template.container_id
                                and current_identity["image"] == template.image
                                and current_identity.get("auto_remove") is not True
                                and current_identity["running"]
                                and runtime.health(template.port, timeout=2.0)
                            ):
                                break
                            time.sleep(1)
                        else:
                            raise DeploymentTemplateInvalid(
                                "direct start completed but the service did not become healthy"
                            )
                        served_model = runtime.smoke_test_service(
                            template.port,
                            model_name=template.model_id,
                            endpoint=template.inference_endpoint,
                            timeout=60,
                        )
                    if template.source_session_id:
                        ai_store.add_audit(
                            template.source_session_id,
                            "deployment_template_reused_directly",
                            "Started the verified model container without an AI session",
                            {
                                "template_id": template.id,
                                "container_id": template.container_id,
                                "served_model": served_model,
                            },
                        )
                    return finish(
                        AIDeploymentReuseRead(
                            reuse_method="direct_script",
                            fallback_to_ai=False,
                            message="已直接执行验证过的启动脚本并完成真实推理检查",
                            template=template,
                        )
                    )
                except DeploymentTemplateBusy:
                    raise
                except Exception as error:  # noqa: BLE001 - direct launch fallback boundary
                    output = (
                        error.output if isinstance(error, DeploymentTemplateExecutionError) else ""
                    )
                    failure_reason = f"直接启动脚本失败：{error}"
                    if output:
                        failure_reason = f"{failure_reason}\n{output[-4000:]}"
                    try:
                        template = template_manager.update_verification(
                            template, "failed", error=failure_reason
                        )
                    except (OSError, ValueError):
                        pass
                    raise DeploymentTemplateInvalid(failure_reason) from error

            if not template.source_session_id:
                raise DeploymentTemplateInvalid("source AI session no longer exists")
            source_session = ai_store.get(template.source_session_id)
            if source_session.task_type != "deployment" or (
                source_session.target_model_id != template.model_id
            ):
                raise DeploymentTemplateInvalid("source AI session identity changed")
            continued = manager.send_instruction(
                source_session.id,
                "用户再次点击了一键部署。请读取 "
                f"{Path(template.script_path).with_name('deployment-template.json')} 中已脱敏的 "
                "launch_command_template，将它作为原始启动命令参考；不要把 start-reusable.sh "
                "的 docker start 包装命令当作原始部署命令。先核验当前模型路径、镜像、"
                "端口和拓扑；不得使用模板之外的凭据，也不得直接回显"
                "环境变量。安全恢复服务后重新完成健康检查和真实推理测试，并提交新的统一"
                "部署结果。如果环境已变化，分析后安全调整。",
            )
            ai_store.add_audit(
                source_session.id,
                "deployment_template_reused",
                "Asked the original AI deployment session to restore its verified template",
                {"template_id": template.id, "reuse_mode": "ai_session"},
            )
            return finish(
                AIDeploymentReuseRead(
                    reuse_method="source_ai_session",
                    fallback_to_ai=False,
                    message="原部署 AI 会话正在按已验证模板恢复服务",
                    template=template,
                    session=continued,
                )
            )
        except DeploymentTemplateBusy as error:
            ai_store.abandon_reuse_request(payload.client_request_id)
            raise HTTPException(
                status_code=409,
                detail=str(error),
                headers={"Retry-After": "1"},
            ) from error
        except AIDeploymentTemplateNotFound:
            pass
        except (
            DeploymentTemplateInvalid,
            AISessionNotFound,
            AISessionBusy,
            AIProviderUnavailable,
            RuntimeErrorBase,
            ValueError,
            OSError,
        ) as error:
            failure_reason = str(error)
        except Exception as error:  # noqa: BLE001 - all reuse failures must fall back to AI
            failure_reason = str(error)

        if template is not None and template.source_session_id:
            try:
                ai_store.add_audit(
                    template.source_session_id,
                    "deployment_template_reuse_failed",
                    "Reusable deployment template could not be applied safely",
                    {"template_id": template.id, "reason": failure_reason},
                )
            except AISessionNotFound:
                pass
            try:
                source_session = ai_store.get(template.source_session_id)
                if source_session.task_type != "deployment" or (
                    source_session.target_model_id != template.model_id
                ):
                    raise DeploymentTemplateInvalid("source AI session identity changed")
                continued = manager.send_instruction(
                    source_session.id,
                    "平台优先直接执行已保存的模型启动脚本，但执行或验证失败。"
                    f"失败原因：{failure_reason}。请结合本会话原始部署上下文检查当前"
                    "容器、模型路径、端口和日志；读取 deployment-template.json 中已脱敏的 "
                    "launch_command_template 作为原始启动命令参考，不要把 start-reusable.sh "
                    "当作原始部署命令。修复后完成健康检查及真实推理测试，"
                    "并重新提交统一部署结果以更新启动/停止脚本。",
                )
                ai_store.add_audit(
                    source_session.id,
                    "deployment_template_reuse_handed_to_ai",
                    "Handed a failed direct launch back to the original AI session",
                    {"template_id": template.id, "reason": failure_reason[-8000:]},
                )
                return finish(
                    AIDeploymentReuseRead(
                        reuse_method="source_ai_session",
                        fallback_to_ai=True,
                        message="直接启动失败，原部署 AI 会话已携带失败日志接管",
                        template=template,
                        session=continued,
                    )
                )
            except (
                AISessionNotFound,
                AISessionBusy,
                AIProviderUnavailable,
                DeploymentTemplateInvalid,
                ValueError,
                RuntimeError,
                OSError,
            ) as original_error:
                failure_reason = f"{failure_reason}；原部署 AI 会话无法接管：{original_error}"
        try:
            fallback = create_reuse_fallback_session(payload, template, failure_reason)
        except AIProviderUnavailable as error:
            ai_store.abandon_reuse_request(payload.client_request_id)
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:
            ai_store.abandon_reuse_request(payload.client_request_id)
            raise HTTPException(status_code=502, detail=str(error)) from error
        return finish(
            AIDeploymentReuseRead(
                reuse_method="new_ai_session",
                fallback_to_ai=True,
                message=(f"复用模板不可用，已带着失败上下文启动新的 AI 部署会话：{failure_reason}"),
                template=template,
                session=fallback,
            )
        )

    @application.get(
        "/api/v1/ai/services",
        response_model=AIDeployedServiceList,
    )
    def list_ai_deployed_services(
        limit: int = Query(default=200, ge=1, le=200),
        include_archived: bool = Query(default=False),
        _: None = Depends(require_ai_enabled),
    ) -> AIDeployedServiceList:
        ai_store: AISessionStore = application.state.ai_store
        runtime: DockerRuntime = application.state.runtime
        _recover_labeled_ai_services(
            ai_store,
            runtime,
            application.state.ai_terminal.sessions_dir,
        )
        items: list[AIDeployedServiceRead] = []
        for result in ai_store.list_results(limit=limit, include_archived=include_archived):
            session = ai_store.get(result.session_id)
            with application.state.ai_service_stop_lock:
                stop_operation = dict(
                    application.state.ai_service_stop_operations.get(result.session_id, {})
                )
            container_running = runtime.is_named_container_running(result.container_name)
            health_reachable = (
                result.status == "verified"
                and container_running
                # SGLang may perform a lightweight distributed readiness step before replying.
                # DGX Spark TP services commonly need about one second even while healthy.
                and runtime.health(result.port, timeout=3.0)
            )
            if result.status != "verified":
                runtime_status = "verification_failed"
            elif health_reachable:
                runtime_status = "running"
            elif container_running:
                runtime_status = "unhealthy"
            else:
                runtime_status = "stopped"
            items.append(
                AIDeployedServiceRead(
                    session_id=result.session_id,
                    linked_deployment_id=session.deployment_id,
                    display_name=session.display_name,
                    target_model_id=session.target_model_id,
                    engine=result.engine,
                    container_name=result.container_name,
                    image=result.image,
                    port=result.port,
                    service_url=result.service_url,
                    launch_command=result.launch_command,
                    result_status=result.status,
                    runtime_status=runtime_status,
                    ai_session_status=session.status,
                    health_reachable=health_reachable,
                    container_running=container_running,
                    stop_status=stop_operation.get("status", "idle"),
                    stop_message=stop_operation.get("message"),
                    stop_session_id=stop_operation.get("stop_session_id"),
                    archived_at=session.archived_at,
                    updated_at=result.updated_at,
                )
            )
        return AIDeployedServiceList(items=items, total=len(items))

    @application.post(
        "/api/v1/ai/services/{session_id}/archive",
        response_model=AISessionRead,
    )
    def archive_ai_deployed_service(
        session_id: str,
        _: None = Depends(require_ai_enabled),
    ) -> AISessionRead:
        store: AISessionStore = application.state.ai_store
        manager: AITerminalManager = application.state.ai_terminal
        runtime: DockerRuntime = application.state.runtime
        try:
            session = store.get(session_id)
            result = store.get_result(session_id)
            if session.task_type != "deployment":
                raise InvalidTransition("service is not owned by an AI deployment session")
            if runtime.is_named_container_running(result.container_name):
                raise InvalidTransition("stop the model service before archiving it")
            archived = manager.archive(session_id)
            application.state.publication_manager.revoke("ai_session", session_id)
            return archived
        except (AISessionNotFound, AIDeploymentResultNotFound) as error:
            raise HTTPException(status_code=404, detail="AI model service not found") from error
        except (AISessionBusy, InvalidTransition) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post(
        "/api/v1/ai/services/{session_id}/restore",
        response_model=AISessionRead,
    )
    def restore_ai_deployed_service(
        session_id: str,
        _: None = Depends(require_ai_enabled),
    ) -> AISessionRead:
        store: AISessionStore = application.state.ai_store
        manager: AITerminalManager = application.state.ai_terminal
        try:
            session = store.get(session_id)
            store.get_result(session_id)
            if session.task_type != "deployment":
                raise InvalidTransition("service is not owned by an AI deployment session")
            return manager.restore(session_id)
        except (AISessionNotFound, AIDeploymentResultNotFound) as error:
            raise HTTPException(status_code=404, detail="AI model service not found") from error
        except InvalidTransition as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    def publication_read_model(value: PublicationStatus) -> ServicePublicationRead:
        publication = value.publication
        external_access_manager: ServiceExternalAccessManager = (
            application.state.external_access_manager
        )
        external_access = external_access_manager.status(
            publication.publication_id if publication else ""
        )
        return ServicePublicationRead(
            target_type=value.target.target_type,
            target_id=value.target.target_id,
            model_id=value.target.model_key,
            display_name=value.target.label,
            publication_id=publication.publication_id if publication else None,
            enabled=bool(publication and publication.enabled),
            base_url=value.base_url,
            key_hint=publication.key_hint if publication and publication.enabled else None,
            gateway_running=value.gateway_running,
            gateway_error=application.state.publication_manager.gateway_error,
            service_running=value.target.running,
            external_access_enabled=external_access.enabled,
            external_url=external_access.public_base_url,
            external_access_state=external_access.tunnel_state,
            external_access_error=external_access.error,
            created_at=publication.created_at if publication else None,
            updated_at=publication.updated_at if publication else None,
        )

    def gateway_token_read(token: GatewayToken) -> GatewayTokenRead:
        remaining = (
            max(token.token_limit - token.used_tokens, 0)
            if token.token_limit is not None
            else None
        )
        return GatewayTokenRead(
            token_id=token.token_id,
            consumer_id=token.consumer_id,
            name=token.name,
            key_hint=token.key_hint,
            enabled=token.enabled and token.consumer_enabled,
            token_limit=token.token_limit,
            used_tokens=token.used_tokens,
            remaining_tokens=remaining,
            expires_at=token.expires_at,
            last_used_at=token.last_used_at,
            created_at=token.created_at,
            updated_at=token.updated_at,
            publication_ids=list(token.publication_ids),
            model_ids=list(token.model_ids),
        )

    def gateway_consumer_read(
        consumer: GatewayConsumer, tokens: list[GatewayToken]
    ) -> GatewayConsumerRead:
        return GatewayConsumerRead(
            consumer_id=consumer.consumer_id,
            name=consumer.name,
            note=consumer.note,
            enabled=consumer.enabled,
            created_at=consumer.created_at,
            updated_at=consumer.updated_at,
            tokens=[gateway_token_read(token) for token in tokens],
        )

    @application.get(
        "/api/v1/gateway/models",
        response_model=list[ServicePublicationRead],
    )
    def gateway_models() -> list[ServicePublicationRead]:
        manager: ServicePublicationManager = application.state.publication_manager
        return [publication_read_model(status) for status in manager.list_statuses()]

    @application.delete(
        "/api/v1/gateway/models/{publication_id}/legacy-key",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def disable_gateway_legacy_key(publication_id: str) -> Response:
        store: ServicePublicationStore = application.state.publication_store
        if not store.disable_legacy_key(publication_id):
            raise HTTPException(status_code=404, detail="shared publication key not found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @application.get("/api/v1/gateway/callers", response_model=GatewayConsumerList)
    def gateway_callers() -> GatewayConsumerList:
        store: GatewayStore = application.state.gateway_store
        items = [
            gateway_consumer_read(consumer, tokens)
            for consumer, tokens in store.list_consumers()
        ]
        return GatewayConsumerList(items=items, total=len(items))

    @application.post(
        "/api/v1/gateway/callers",
        response_model=GatewayConsumerRead,
        status_code=status.HTTP_201_CREATED,
    )
    def create_gateway_caller(request: GatewayConsumerCreate) -> GatewayConsumerRead:
        store: GatewayStore = application.state.gateway_store
        consumer = store.create_consumer(request.name, request.note)
        return gateway_consumer_read(consumer, [])

    @application.delete(
        "/api/v1/gateway/callers/{consumer_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def disable_gateway_caller(consumer_id: str) -> Response:
        store: GatewayStore = application.state.gateway_store
        if not store.disable_consumer(consumer_id):
            raise HTTPException(status_code=404, detail="API caller not found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @application.delete(
        "/api/v1/gateway/callers/{consumer_id}/permanent",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def delete_gateway_caller(consumer_id: str) -> Response:
        store: GatewayStore = application.state.gateway_store
        if not store.delete_consumer(consumer_id):
            raise HTTPException(status_code=404, detail="API caller not found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @application.post(
        "/api/v1/gateway/callers/{consumer_id}/tokens",
        response_model=GatewayTokenSecret,
        status_code=status.HTTP_201_CREATED,
    )
    def create_gateway_token(
        consumer_id: str,
        request: GatewayTokenCreate,
        response: Response,
    ) -> GatewayTokenSecret:
        store: GatewayStore = application.state.gateway_store
        expires_at = request.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at is not None and expires_at <= datetime.now(UTC):
            raise HTTPException(status_code=422, detail="expires_at must be in the future")
        try:
            token, api_key = store.create_token(
                consumer_id,
                request.name,
                request.publication_ids,
                token_limit=request.token_limit,
                expires_at=expires_at,
            )
        except GatewayConsumerNotFound as error:
            raise HTTPException(status_code=404, detail="API caller not found") from error
        except GatewayPublicationScopeInvalid as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        response.headers["Cache-Control"] = "no-store"
        return GatewayTokenSecret(**gateway_token_read(token).model_dump(), api_key=api_key)

    @application.delete(
        "/api/v1/gateway/tokens/{token_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def disable_gateway_token(token_id: str) -> Response:
        store: GatewayStore = application.state.gateway_store
        if not store.disable_token(token_id):
            try:
                store.get_token(token_id)
            except GatewayTokenNotFound as error:
                raise HTTPException(status_code=404, detail="API key not found") from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @application.get("/api/v1/gateway/usage", response_model=GatewayUsagePage)
    def gateway_usage(
        publication_id: str | None = Query(default=None, max_length=64),
        consumer_id: str | None = Query(default=None, max_length=64),
        token_id: str | None = Query(default=None, max_length=64),
        limit: int = Query(default=100, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> GatewayUsagePage:
        store: GatewayStore = application.state.gateway_store
        items, total, summary = store.usage_page(
            publication_id=publication_id,
            consumer_id=consumer_id,
            token_id=token_id,
            limit=limit,
            offset=offset,
        )
        return GatewayUsagePage(
            items=[GatewayUsageRead(**item) for item in items],
            total=total,
            summary=GatewayUsageSummary(**summary),
        )

    @application.get(
        "/api/v1/service-publications/{target_type}/{target_id}",
        response_model=ServicePublicationRead,
    )
    def service_publication_status(
        target_type: str,
        target_id: str,
    ) -> ServicePublicationRead:
        manager: ServicePublicationManager = application.state.publication_manager
        try:
            return publication_read_model(manager.status(target_type, target_id))
        except PublicationTargetNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @application.post(
        "/api/v1/service-publications/{target_type}/{target_id}",
        response_model=ServicePublicationSecret,
        status_code=status.HTTP_201_CREATED,
    )
    def publish_model_service(
        target_type: str,
        target_id: str,
        response: Response,
    ) -> ServicePublicationSecret:
        manager: ServicePublicationManager = application.state.publication_manager
        try:
            if target_type == "ai_session":
                with application.state.ai_service_stop_lock:
                    stop_operation = application.state.ai_service_stop_operations.get(target_id)
                if stop_operation and stop_operation.get("status") in {
                    "direct_script",
                    "original_session",
                    "fallback_session",
                }:
                    raise PublicationTargetUnavailable("model service is currently being stopped")
            publication, api_key = manager.publish(target_type, target_id)
            response.headers["Cache-Control"] = "no-store"
            return ServicePublicationSecret(
                **publication_read_model(publication).model_dump(),
                api_key=api_key,
            )
        except PublicationTargetNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (PublicationTargetUnavailable, PublicationAlreadyEnabled) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except PublicationGatewayUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @application.post(
        "/api/v1/service-publications/{target_type}/{target_id}/rotate-key",
        response_model=ServicePublicationSecret,
    )
    def rotate_model_service_key(
        target_type: str,
        target_id: str,
        response: Response,
    ) -> ServicePublicationSecret:
        manager: ServicePublicationManager = application.state.publication_manager
        try:
            publication, api_key = manager.rotate_key(target_type, target_id)
            response.headers["Cache-Control"] = "no-store"
            return ServicePublicationSecret(
                **publication_read_model(publication).model_dump(),
                api_key=api_key,
            )
        except PublicationTargetNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PublicationNotEnabled as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except PublicationGatewayUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @application.delete(
        "/api/v1/service-publications/{target_type}/{target_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def revoke_model_service_publication(
        target_type: str,
        target_id: str,
    ) -> Response:
        manager: ServicePublicationManager = application.state.publication_manager
        try:
            publication = manager.store.get_for_target(target_type, target_id)
            manager.revoke(target_type, target_id)
            if publication is not None:
                try:
                    application.state.external_access_manager.stop(publication.publication_id)
                except Exception:  # noqa: BLE001 - LAN revocation is authoritative
                    LOGGER.exception(
                        "could not disable external access for revoked publication %s",
                        publication.publication_id,
                    )
        except PublicationTargetNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    def _enabled_publication_or_error(target_type: str, target_id: str) -> str:
        publication = application.state.publication_store.get_for_target(target_type, target_id)
        if publication is None or not publication.enabled:
            raise HTTPException(
                status_code=409,
                detail="enable authenticated LAN access before external access",
            )
        return publication.publication_id

    @application.get(
        "/api/v1/service-publications/{target_type}/{target_id}/external-access",
        response_model=ServicePublicationRead,
    )
    def external_access_status(
        target_type: str,
        target_id: str,
    ) -> ServicePublicationRead:
        manager: ServicePublicationManager = application.state.publication_manager
        try:
            return publication_read_model(manager.status(target_type, target_id))
        except PublicationTargetNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @application.post(
        "/api/v1/service-publications/{target_type}/{target_id}/external-access",
        response_model=ServicePublicationRead,
    )
    def enable_external_access(
        target_type: str,
        target_id: str,
    ) -> ServicePublicationRead:
        publication_id = _enabled_publication_or_error(target_type, target_id)
        try:
            application.state.external_access_manager.start(publication_id)
        except ExternalAccessUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except ExternalAccessStartError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        manager: ServicePublicationManager = application.state.publication_manager
        try:
            return publication_read_model(manager.status(target_type, target_id))
        except PublicationTargetNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @application.delete(
        "/api/v1/service-publications/{target_type}/{target_id}/external-access",
        response_model=ServicePublicationRead,
    )
    def disable_external_access(
        target_type: str,
        target_id: str,
    ) -> ServicePublicationRead:
        publication_id = _enabled_publication_or_error(target_type, target_id)
        application.state.external_access_manager.stop(publication_id)
        manager: ServicePublicationManager = application.state.publication_manager
        try:
            return publication_read_model(manager.status(target_type, target_id))
        except PublicationTargetNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @application.post("/api/v1/ai/services/{session_id}/stop", status_code=202)
    def request_ai_service_stop(
        session_id: str,
        _: None = Depends(require_ai_enabled),
    ) -> dict[str, str | None]:
        store: AISessionStore = application.state.ai_store
        runtime: DockerRuntime = application.state.runtime
        manager: AITerminalManager = application.state.ai_terminal
        try:
            session = store.get(session_id)
            result = store.get_result(session_id)
            if session.task_type != "deployment":
                raise HTTPException(
                    status_code=409,
                    detail="service is not owned by an AI deployment session",
                )
            if not runtime.is_named_container_running(result.container_name):
                raise HTTPException(status_code=409, detail="model service is not running")
            direct_template: AIDeploymentTemplateRead | None = None
            direct_failure: str | None = None
            is_multi_node = False
            try:
                candidate = store.get_template(session_id)
                is_multi_node = candidate.deployment_topology == "multi_node"
                application.state.ai_deployment_templates.validate(candidate)
                if candidate.reuse_mode == "direct_script":
                    direct_template = candidate
            except (AIDeploymentTemplateNotFound, DeploymentTemplateInvalid) as error:
                direct_failure = str(error)
            if not is_multi_node:
                try:
                    context_topology = manager.model_context(session_id).get("deployment_topology")
                    if isinstance(context_topology, dict):
                        context_topology = context_topology.get("resolved") or context_topology.get(
                            "requested"
                        )
                    is_multi_node = str(context_topology) == "multi_node"
                except (OSError, ValueError, TypeError, AttributeError):
                    pass
            with application.state.ai_service_stop_lock:
                current_operation = application.state.ai_service_stop_operations.get(session_id)
                if current_operation and current_operation.get("status") in {
                    "direct_script",
                    "original_session",
                    "fallback_session",
                }:
                    return {
                        "status": str(current_operation["status"]),
                        "session_id": session_id,
                        "stop_session_id": current_operation.get("stop_session_id"),
                    }
            runtime_context: dict[str, object] = {
                "container_name": result.container_name,
                "container_id": result.container_id,
                "image": result.image,
                "port": result.port,
                "service_url": result.service_url,
                "launch_command": result.launch_command,
                "container_running": True,
                "health_reachable": runtime.health(result.port, timeout=0.5),
            }
            try:
                runtime_context["runtime_profile"] = (
                    application.state.arena_manager.runtime_profile(
                        "ai_session", session_id
                    ).model_dump(mode="json")
                )
            except Exception:  # noqa: BLE001 - snapshot still useful without profile
                runtime_context["runtime_profile"] = None
            snapshot_path, _ = manager.backup_service_context(
                session_id, runtime_context=runtime_context
            )
            store.add_audit(
                session_id,
                "service_stop_requested",
                "User requested the deployed model service to stop",
                {
                    "container_name": result.container_name,
                    "port": result.port,
                    "snapshot_path": str(snapshot_path),
                },
            )
            if direct_template is not None:
                with application.state.ai_service_stop_lock:
                    current_operation = application.state.ai_service_stop_operations.get(session_id)
                    if current_operation and current_operation.get("status") in {
                        "direct_script",
                        "original_session",
                        "fallback_session",
                    }:
                        return {
                            "status": str(current_operation["status"]),
                            "session_id": session_id,
                            "stop_session_id": current_operation.get("stop_session_id"),
                        }
                    application.state.ai_service_stop_operations[session_id] = {
                        "status": "direct_script",
                        "message": "正在执行已验证的停止脚本…",
                        "stop_session_id": None,
                    }
                try:
                    with application.state.ai_deployment_templates.operation(direct_template.id):
                        application.state.publication_manager.revoke("ai_session", session_id)
                        identity = runtime.named_container_identity(direct_template.container_name)
                        if identity is None:
                            raise DeploymentTemplateInvalid(
                                "the verified model container no longer exists"
                            )
                        if identity["container_id"] != direct_template.container_id:
                            raise DeploymentTemplateInvalid(
                                "the verified model container identity changed"
                            )
                        if identity["image"] != direct_template.image:
                            raise DeploymentTemplateInvalid(
                                "the verified model container image changed"
                            )
                        if identity.get("auto_remove") is True:
                            raise DeploymentTemplateInvalid(
                                "the verified model container uses auto-remove"
                            )
                        application.state.ai_deployment_templates.execute(direct_template, "stop")
                        stop_deadline = time.monotonic() + 45
                        while time.monotonic() < stop_deadline:
                            if not runtime.is_named_container_running(
                                direct_template.container_name
                            ) and not runtime.health(direct_template.port, timeout=0.5):
                                break
                            time.sleep(0.5)
                        else:
                            raise DeploymentTemplateInvalid(
                                "stop artifact returned but the model service is still reachable"
                            )
                    store.add_audit(
                        session_id,
                        "service_stopped_directly",
                        "Stopped the model service with its verified local artifact",
                        {
                            "template_id": direct_template.id,
                            "container_id": direct_template.container_id,
                        },
                    )
                    with application.state.ai_service_stop_lock:
                        application.state.ai_service_stop_operations[session_id] = {
                            "status": "completed",
                            "message": "已执行停止脚本并确认服务端口不可达",
                            "stop_session_id": None,
                        }
                    return {
                        "status": "completed",
                        "session_id": session_id,
                        "stop_session_id": None,
                    }
                except DeploymentTemplateBusy as error:
                    with application.state.ai_service_stop_lock:
                        application.state.ai_service_stop_operations[session_id] = {
                            "status": "failed",
                            "message": "模型服务正在执行其他生命周期操作，请稍后重试",
                            "stop_session_id": None,
                        }
                    raise HTTPException(
                        status_code=409,
                        detail=str(error),
                        headers={"Retry-After": "1"},
                    ) from error
                except Exception as error:  # noqa: BLE001 - direct stop fallback boundary
                    output = (
                        error.output if isinstance(error, DeploymentTemplateExecutionError) else ""
                    )
                    direct_failure = f"直接停止脚本失败：{error}"
                    if output:
                        direct_failure = f"{direct_failure}\n{output[-4000:]}"
                    try:
                        application.state.ai_deployment_templates.update_verification(
                            direct_template, "failed", error=direct_failure
                        )
                    except (OSError, ValueError):
                        pass
                    store.add_audit(
                        session_id,
                        "service_stop_direct_failed",
                        "Direct stop failed; handing the task to AI",
                        {"reason": direct_failure[-8000:]},
                    )
                    with application.state.ai_service_stop_lock:
                        application.state.ai_service_stop_operations[session_id] = {
                            "status": "original_session",
                            "message": "停止脚本失败，正在由原部署 AI 会话接管…",
                            "stop_session_id": session_id,
                        }
            else:
                application.state.publication_manager.revoke("ai_session", session_id)
                with application.state.ai_service_stop_lock:
                    current_operation = application.state.ai_service_stop_operations.get(session_id)
                    if current_operation and current_operation.get("status") in {
                        "direct_script",
                        "original_session",
                        "fallback_session",
                    }:
                        return {
                            "status": str(current_operation["status"]),
                            "session_id": session_id,
                            "stop_session_id": current_operation.get("stop_session_id"),
                        }
                    application.state.ai_service_stop_operations[session_id] = {
                        "status": "original_session",
                        "message": "正在由原部署 AI 会话停止服务…",
                        "stop_session_id": session_id,
                    }
            prompt = (
                "用户已在平台点击‘停止服务’。请立即停止这个部署会话创建的模型服务，"
                f"当前报告的主容器是 {result.container_name}，服务端口是 {result.port}。"
                "请结合本会话原有上下文，停止该部署创建的全部相关推理进程和容器；如果是"
                "多节点部署，也要安全停止对应的远端 rank。仅操作本次部署，绝对不要停止 "
                "Uniform 平台、其他模型服务或无关容器。完成后验证容器已停止且原服务端口"
                "不可达。不要重新部署或重启模型。这是停服任务，不是部署任务。"
            )
            if direct_failure:
                prompt = f"{prompt}\n平台直接停止尝试未完成：{direct_failure}"
            original_error: str | None = None
            try:
                manager.send_instruction(session_id, prompt)
            except Exception as error:  # noqa: BLE001 - any delivery failure uses the snapshot
                original_error = str(error)
                with application.state.ai_service_stop_lock:
                    application.state.ai_service_stop_operations[session_id] = {
                        "status": "fallback_session",
                        "message": "原部署会话不可用，正在由备份上下文接管…",
                        "stop_session_id": None,
                    }

            def update_operation(
                operation_status: str,
                message: str,
                stop_session_id: str | None = None,
            ) -> None:
                with application.state.ai_service_stop_lock:
                    application.state.ai_service_stop_operations[session_id] = {
                        "status": operation_status,
                        "message": message,
                        "stop_session_id": stop_session_id,
                    }

            def wait_until_stopped(seconds: int, watched_session_id: str) -> str:
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    main_service_stopped = not runtime.is_named_container_running(
                        result.container_name
                    ) and not runtime.health(result.port, timeout=0.5)
                    try:
                        watched = store.get(watched_session_id)
                    except AISessionNotFound:
                        watched_ended = True
                        watched_completed = False
                    else:
                        watched_completed = watched.status == "exited"
                        watched_ended = watched.status in {
                            "exited",
                            "failed",
                            "stopped",
                            "interrupted",
                        }
                    if main_service_stopped and (not is_multi_node or watched_completed):
                        return "stopped"
                    if watched_ended:
                        return "ended"
                    time.sleep(1)
                return "timeout"

            def continue_stop_in_background() -> None:
                original_outcome = "ended" if original_error else wait_until_stopped(45, session_id)
                if original_outcome == "stopped":
                    update_operation(
                        "completed",
                        (
                            "主服务已关闭，AI 已完成远端停服流程"
                            if is_multi_node
                            else "主容器已停止且服务端口不可达"
                        ),
                        session_id,
                    )
                    return
                if original_outcome == "timeout":
                    try:
                        manager.stop(session_id)
                        store.add_audit(
                            session_id,
                            "service_stop_original_timed_out",
                            "Stopped the unresponsive original AI session before handoff",
                            {"timeout_seconds": 45},
                        )
                    except (AISessionNotFound, OSError):
                        pass
                update_operation(
                    "fallback_session",
                    "原部署会话不可用，正在由备份上下文创建停服 AI 会话…",
                )
                try:
                    snapshot_path_latest, snapshot_latest = manager.backup_service_context(
                        session_id, runtime_context=runtime_context
                    )
                    fallback = manager.create(
                        AISessionCreate(
                            provider=session.provider,
                            task_type="service_stop",
                            display_name=f"{session.display_name} · 停服恢复",
                            ai_model=session.ai_model,
                            reasoning_effort=session.reasoning_effort,
                            execution_mode="full_access",
                            full_access_acknowledged=True,
                            initial_prompt=(
                                "这是一次独立的停服恢复任务。原部署 AI 会话已经退出或不可"
                                "用。先阅读 deployment-context.json 中的 service_handoff，"
                                f"并读取持久化快照 {snapshot_path_latest} 及其中列出的上下文"
                                "文件。只停止该快照对应的模型服务；若为多节点部署，停止"
                                "全部 rank。不得停止 Uniform 平台或其他服务，不得重新部署。"
                                "完成后验证所有相关容器/进程退出且服务端口不可达。"
                            ),
                        ),
                        deployment_context={"service_handoff": snapshot_latest},
                    )
                    store.add_audit(
                        session_id,
                        "service_stop_fallback_started",
                        "Started a new AI session from the durable service snapshot",
                        {
                            "fallback_session_id": fallback.id,
                            "snapshot_path": str(snapshot_path_latest),
                            "original_error": original_error,
                        },
                    )
                    update_operation(
                        "fallback_session",
                        "新的停服 AI 会话已接管，正在停止并验证所有节点…",
                        fallback.id,
                    )
                    fallback_outcome = wait_until_stopped(600, fallback.id)
                    if fallback_outcome == "stopped":
                        update_operation(
                            "completed",
                            (
                                "主服务已关闭，停服恢复 AI 已完成远端停服流程"
                                if is_multi_node
                                else "主容器已停止且服务端口不可达"
                            ),
                            fallback.id,
                        )
                    else:
                        update_operation(
                            "failed",
                            "停服 AI 会话已结束，但模型容器仍在运行",
                            fallback.id,
                        )
                except Exception as error:  # noqa: BLE001 - background operation boundary
                    update_operation("failed", f"创建停服恢复会话失败：{error}")

            threading.Thread(
                target=continue_stop_in_background,
                name=f"ai-service-stop-{session_id[:8]}",
                daemon=True,
            ).start()
            return {
                "status": "original_session" if original_error is None else "fallback_session",
                "session_id": session_id,
                "stop_session_id": session_id if original_error is None else None,
            }
        except AISessionNotFound as error:
            raise HTTPException(status_code=404, detail="AI session not found") from error
        except AIDeploymentResultNotFound as error:
            raise HTTPException(status_code=404, detail="deployment result not reported") from error
        except (AIProviderUnavailable, AISessionBusy, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    @application.get("/api/v1/ai/sessions/{session_id}/transcript")
    def ai_session_transcript(
        session_id: str,
        offset: int = Query(default=0, ge=0),
        _: None = Depends(require_ai_enabled),
    ) -> Response:
        manager: AITerminalManager = application.state.ai_terminal
        try:
            data, next_offset = manager.transcript(session_id, offset=offset)
        except AISessionNotFound as error:
            raise HTTPException(status_code=404, detail="AI session not found") from error
        return Response(
            data,
            media_type="application/octet-stream",
            headers={"X-Transcript-Offset": str(next_offset)},
        )

    @application.post("/api/v1/ai/sessions/{session_id}/stop", response_model=AISessionRead)
    def stop_ai_session(
        session_id: str,
        _: None = Depends(require_ai_enabled),
    ) -> AISessionRead:
        manager: AITerminalManager = application.state.ai_terminal
        try:
            return manager.stop(session_id)
        except AISessionNotFound as error:
            raise HTTPException(status_code=404, detail="AI session not found") from error

    @application.get(
        "/api/v1/ai/sessions/{session_id}/auto-resume",
        response_model=AIAutoResumeRead,
    )
    def get_ai_auto_resume(
        session_id: str,
        _: None = Depends(require_ai_enabled),
    ) -> AIAutoResumeRead:
        manager: AITerminalManager = application.state.ai_terminal
        try:
            return manager.auto_resume_status(session_id)
        except AISessionNotFound as error:
            raise HTTPException(status_code=404, detail="AI session not found") from error

    @application.post(
        "/api/v1/ai/sessions/{session_id}/auto-resume",
        response_model=AIAutoResumeRead,
    )
    def configure_ai_auto_resume(
        session_id: str,
        payload: AIAutoResumeUpdate,
        _: None = Depends(require_ai_enabled),
    ) -> AIAutoResumeRead:
        manager: AITerminalManager = application.state.ai_terminal
        try:
            return manager.configure_auto_resume(session_id, payload)
        except AISessionNotFound as error:
            raise HTTPException(status_code=404, detail="AI session not found") from error
        except AISessionBusy as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    def verify_ai_capability(
        session_id: str,
        manager: AITerminalManager,
        token: str,
    ) -> AISessionRead:
        try:
            session = application.state.ai_store.get(session_id)
        except AISessionNotFound as error:
            raise HTTPException(status_code=404, detail="AI session not found") from error
        if not manager.validate_capability(session_id, token):
            raise HTTPException(status_code=403, detail="invalid AI capability")
        if session.task_type in {"accuracy_evaluation", "dataset_integration"}:
            raise HTTPException(
                status_code=403, detail="accuracy capability cannot control deployment"
            )
        return session

    @application.post(
        "/api/v1/ai/sessions/{session_id}/actions/deploy",
        response_model=DeploymentRead,
    )
    def ai_deploy_action(
        session_id: str,
        payload: AIDeploymentAction,
        x_ai_capability: str = Header(default="", alias="X-AI-Capability"),
    ) -> DeploymentRead:
        manager: AITerminalManager = application.state.ai_terminal
        verify_ai_capability(session_id, manager, x_ai_capability)
        context = manager.model_context(session_id)
        deployment_context = context.get("deployment")
        target = (
            deployment_context.get("target_model") if isinstance(deployment_context, dict) else None
        )
        if not isinstance(target, dict) or not target.get("model_id"):
            raise HTTPException(status_code=409, detail="session has no target model")
        parameters = DeploymentParameters(
            source=(
                target["source"]
                if target.get("source") in {"huggingface", "modelscope"}
                else "huggingface"
            ),
            image=payload.image,
            port=payload.port,
            served_model_name=payload.served_model_name,
            max_model_len=payload.max_model_len,
            gpu_memory_utilization=payload.gpu_memory_utilization,
            startup_timeout_seconds=payload.startup_timeout_seconds,
            trust_remote_code=payload.trust_remote_code,
            remote_code_acknowledged=payload.remote_code_acknowledged,
            extra_args=payload.extra_args,
            local_model_path=target.get("local_model_path"),
        )
        deployment_store: DeploymentStore = application.state.store
        deployment = deployment_store.create(
            DeploymentCreate(
                model_id=target["model_id"],
                revision=target.get("revision"),
                display_name=f"AI · {target['model_id']}",
                parameters=parameters,
            )
        )
        application.state.ai_store.link_deployment(session_id, deployment.id)
        if resolved_settings.worker_enabled:
            application.state.worker.wake()
        return deployment

    @application.get(
        "/api/v1/ai/sessions/{session_id}/actions/deployment",
        response_model=DeploymentRead,
    )
    def ai_deployment_status(
        session_id: str,
        x_ai_capability: str = Header(default="", alias="X-AI-Capability"),
    ) -> DeploymentRead:
        manager: AITerminalManager = application.state.ai_terminal
        session = verify_ai_capability(session_id, manager, x_ai_capability)
        if not session.deployment_id:
            raise HTTPException(status_code=409, detail="AI has not created a deployment yet")
        return application.state.store.get(session.deployment_id)

    @application.get("/api/v1/ai/sessions/{session_id}/actions/logs")
    def ai_deployment_logs(
        session_id: str,
        x_ai_capability: str = Header(default="", alias="X-AI-Capability"),
    ) -> dict[str, str]:
        manager: AITerminalManager = application.state.ai_terminal
        session = verify_ai_capability(session_id, manager, x_ai_capability)
        if not session.deployment_id:
            raise HTTPException(status_code=409, detail="AI has not created a deployment yet")
        try:
            return {"logs": application.state.runtime.logs(session.deployment_id, tail=500)}
        except DockerCommandError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    def _record_reusable_deployment_template_locked(
        session_id: str, result: AIDeploymentResultRead
    ) -> AIDeploymentTemplateRead | None:
        if result.status != "verified":
            return None
        ai_store: AISessionStore = application.state.ai_store
        manager: AITerminalManager = application.state.ai_terminal
        template_manager: AIDeploymentTemplateManager = application.state.ai_deployment_templates
        session = ai_store.get(session_id)
        deployment: DeploymentRead | None = None
        if session.deployment_id:
            try:
                deployment = application.state.store.get(session.deployment_id)
            except DeploymentNotFound:
                deployment = None
        try:
            template = template_manager.create(
                session,
                result,
                deployment_context=manager.model_context(session_id),
                deployment=deployment,
            )
        except (DeploymentTemplateInvalid, OSError, ValueError) as error:
            ai_store.add_audit(
                session_id,
                "deployment_template_failed",
                "Could not create the reusable deployment template",
                {"reason": str(error)},
            )
            return None
        if template.artifact_verification_status != "pending":
            return template

        runtime: DockerRuntime = application.state.runtime

        def assert_container_identity(*, must_be_running: bool | None = None) -> bool:
            identity = runtime.named_container_identity(template.container_name)
            if identity is None:
                raise DeploymentTemplateInvalid("the reported model container no longer exists")
            if identity["container_id"] != template.container_id:
                raise DeploymentTemplateInvalid("the model container identity changed")
            if identity["image"] != template.image:
                raise DeploymentTemplateInvalid("the model container image changed")
            if identity.get("auto_remove") is True:
                raise DeploymentTemplateInvalid(
                    "the model container uses auto-remove and cannot be certified"
                )
            running = bool(identity["running"])
            if must_be_running is not None and running != must_be_running:
                expected = "running" if must_be_running else "stopped"
                raise DeploymentTemplateInvalid(f"the model container is not {expected}")
            return running

        def wait_until_ready(timeout: int) -> None:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if assert_container_identity() and runtime.health(template.port, timeout=2.0):
                    return
                time.sleep(1)
            raise DeploymentTemplateInvalid(
                f"model service did not become healthy within {timeout} seconds"
            )

        def restore_after_failure(current: AIDeploymentTemplateRead) -> str | None:
            try:
                identity = runtime.named_container_identity(current.container_name)
                if identity is None or identity["container_id"] != current.container_id:
                    return "container identity changed; automatic recovery was skipped"
                if identity["image"] != current.image:
                    return "container image changed; automatic recovery was skipped"
                if identity.get("auto_remove") is True:
                    return "auto-remove container; automatic recovery was skipped"
                if not identity["running"]:
                    template_manager.execute(current, "start", allow_unverified=True)
                wait_until_ready(resolved_settings.default_startup_timeout_seconds)
                runtime.smoke_test_service(
                    current.port,
                    model_name=session.target_model_id,
                    endpoint=current.inference_endpoint,
                    timeout=60,
                )
                return None
            except Exception as recovery_error:  # noqa: BLE001 - recovery is best effort
                return str(recovery_error)

        def certify_artifacts() -> AIDeploymentTemplateRead:
            verifying = template_manager.update_verification(template, "verifying")
            stage = "preflight"
            try:
                assert_container_identity(must_be_running=True)
                if not runtime.health(verifying.port, timeout=3.0):
                    raise DeploymentTemplateInvalid("reported service health check is unreachable")
                runtime.smoke_test_service(
                    verifying.port,
                    model_name=session.target_model_id,
                    endpoint=verifying.inference_endpoint,
                    timeout=60,
                )
                stage = "stop"
                template_manager.execute(verifying, "stop", allow_unverified=True)
                stop_deadline = time.monotonic() + 45
                while time.monotonic() < stop_deadline:
                    if not assert_container_identity() and not runtime.health(
                        verifying.port, timeout=0.5
                    ):
                        break
                    time.sleep(0.5)
                else:
                    raise DeploymentTemplateInvalid(
                        "stop artifact did not stop the container and close the service port"
                    )
                stage = "start"
                template_manager.execute(verifying, "start", allow_unverified=True)
                stage = "readiness"
                wait_until_ready(resolved_settings.default_startup_timeout_seconds)
                stage = "inference"
                served_model = runtime.smoke_test_service(
                    verifying.port,
                    model_name=session.target_model_id,
                    endpoint=verifying.inference_endpoint,
                    timeout=60,
                )
                verified = template_manager.update_verification(verifying, "verified")
                ai_store.add_audit(
                    session_id,
                    "deployment_artifacts_verified",
                    "Verified stop and start artifacts with a real inference request",
                    {
                        "template_id": verified.id,
                        "container_id": verified.container_id,
                        "served_model": served_model,
                    },
                )
                return verified
            except Exception as error:  # noqa: BLE001 - lifecycle certification boundary
                output = error.output if isinstance(error, DeploymentTemplateExecutionError) else ""
                recovery_error = restore_after_failure(verifying)
                reason = f"{stage}: {error}"
                if output:
                    reason = f"{reason}\n{output[-4000:]}"
                if recovery_error:
                    reason = f"{reason}\nrecovery: {recovery_error}"
                template_manager.update_verification(verifying, "failed", error=reason)
                ai_store.add_audit(
                    session_id,
                    "deployment_artifact_verification_failed",
                    "Reusable lifecycle artifact verification failed",
                    {"stage": stage, "reason": reason[-8000:]},
                )
                raise DeploymentTemplateInvalid(reason) from error

        return certify_artifacts()

    def record_reusable_deployment_template(
        session_id: str, result: AIDeploymentResultRead
    ) -> AIDeploymentTemplateRead | None:
        if result.status != "verified":
            return None
        template_manager: AIDeploymentTemplateManager = application.state.ai_deployment_templates
        # Serialize creation and the complete stop/start certification. This prevents two
        # simultaneous reports from replacing the fixed artifacts while either is in use.
        with template_manager.operation(session_id):
            return _record_reusable_deployment_template_locked(session_id, result)

    @application.post(
        "/api/v1/ai/sessions/{session_id}/actions/result",
        response_model=AIDeploymentResultRead,
    )
    def ai_report_deployment_result(
        session_id: str,
        payload: AIDeploymentResultCreate,
        x_ai_capability: str = Header(default="", alias="X-AI-Capability"),
    ) -> AIDeploymentResultRead:
        manager: AITerminalManager = application.state.ai_terminal
        verify_ai_capability(session_id, manager, x_ai_capability)
        reported = application.state.ai_store.upsert_result(session_id, payload)
        try:
            manager.backup_service_context(
                session_id,
                runtime_context={
                    "container_name": payload.container_name,
                    "container_id": payload.container_id,
                    "image": payload.image,
                    "port": payload.port,
                    "service_url": payload.service_url,
                    "launch_command": payload.launch_command,
                },
            )
        except OSError:
            pass
        try:
            record_reusable_deployment_template(session_id, reported)
        except DeploymentTemplateBusy as error:
            raise HTTPException(
                status_code=409,
                detail=str(error),
                headers={"Retry-After": "1"},
            ) from error
        except DeploymentTemplateInvalid as error:
            raise HTTPException(
                status_code=409,
                detail=(
                    "模型服务结果已记录，但启动/停止脚本的复现验证失败；"
                    f"请根据诊断继续修复并重新提交结果：{error}"
                ),
            ) from error
        return reported

    @application.post(
        "/api/v1/ai/sessions/{session_id}/actions/result/managed",
        response_model=AIDeploymentResultRead,
    )
    def ai_report_managed_deployment_result(
        session_id: str,
        x_ai_capability: str = Header(default="", alias="X-AI-Capability"),
    ) -> AIDeploymentResultRead:
        manager: AITerminalManager = application.state.ai_terminal
        session = verify_ai_capability(session_id, manager, x_ai_capability)
        if not session.deployment_id:
            raise HTTPException(status_code=409, detail="AI has not created a deployment yet")
        deployment: DeploymentRead = application.state.store.get(session.deployment_id)
        if deployment.status.value != "running":
            raise HTTPException(
                status_code=409,
                detail=f"managed deployment is not running: {deployment.status.value}",
            )
        runtime: DockerRuntime = application.state.runtime
        try:
            metadata = runtime.container_metadata(deployment)
        except DockerCommandError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        port = deployment.parameters.port
        model_name = deployment.parameters.served_model_name or deployment.model_id
        result = AIDeploymentResultCreate(
            engine="vllm",
            container_name=str(metadata["container_name"]),
            container_id=metadata["container_id"],
            image=deployment.parameters.image,
            port=port,
            service_url=f"http://127.0.0.1:{port}",
            launch_command=str(metadata["launch_command"]),
            health_check=AIHealthCheckResult(
                method="GET",
                url=f"http://127.0.0.1:{port}/health",
                status_code=200,
                passed=True,
                details="Health check passed before deployment entered running state.",
            ),
            inference_test=AIInferenceTestResult(
                method="POST",
                url=f"http://127.0.0.1:{port}/v1/completions",
                status_code=200,
                passed=True,
                request_summary=f"model={model_name!r}, prompt='test', max_tokens=1",
                response_summary="OpenAI-compatible response contained at least one choice.",
            ),
        )
        reported = application.state.ai_store.upsert_result(session_id, result)
        try:
            manager.backup_service_context(
                session_id,
                runtime_context={
                    "container_name": result.container_name,
                    "container_id": result.container_id,
                    "image": result.image,
                    "port": result.port,
                    "service_url": result.service_url,
                    "launch_command": result.launch_command,
                },
            )
        except OSError:
            pass
        try:
            record_reusable_deployment_template(session_id, reported)
        except DeploymentTemplateBusy as error:
            raise HTTPException(
                status_code=409,
                detail=str(error),
                headers={"Retry-After": "1"},
            ) from error
        return reported

    @application.websocket("/api/v1/ai/sessions/{session_id}/terminal")
    async def ai_terminal_socket(websocket: WebSocket, session_id: str) -> None:
        await websocket.accept()
        try:
            authentication = await asyncio.wait_for(websocket.receive_json(), timeout=5)
            supplied = str(authentication.get("token", ""))
            if authentication.get("type") != "auth" or (
                resolved_settings.api_token
                and not hmac.compare_digest(supplied, resolved_settings.api_token)
            ):
                await websocket.close(code=4401, reason="invalid API key")
                return
            if not resolved_settings.ai_sessions_enabled:
                await websocket.close(code=4403, reason="AI sessions are disabled")
                return
            manager: AITerminalManager = application.state.ai_terminal
            ai_store: AISessionStore = application.state.ai_store
            ai_store.get(session_id)
            ai_store.add_audit(session_id, "terminal_connected", "Web terminal connected")
            history_size = manager.transcript_size(session_id)
            history_limit = 2_000_000
            history_start = max(0, history_size - history_limit)
            await websocket.send_json(
                {
                    "type": "ready",
                    "history_bytes": history_size - history_start,
                    "history_truncated": history_start > 0,
                }
            )
            offset = history_start
            last_status = ""
            while offset < history_size:
                data, next_offset = manager.transcript(session_id, offset=offset)
                data = data[: history_size - offset]
                if not data:
                    break
                await websocket.send_bytes(data)
                offset += len(data)
                if next_offset <= offset and offset < history_size:
                    break
            await websocket.send_json({"type": "history_end", "offset": offset})
            while True:
                data, offset = manager.transcript(session_id, offset=offset)
                if data:
                    await websocket.send_bytes(data)
                current_status = ai_store.get(session_id).status
                if current_status != last_status:
                    await websocket.send_json({"type": "status", "status": current_status})
                    last_status = current_status
                try:
                    message = await asyncio.wait_for(websocket.receive_json(), timeout=0.1)
                except TimeoutError:
                    continue
                message_type = message.get("type")
                if message_type == "input":
                    manager.write(session_id, str(message.get("data", "")))
                elif message_type == "resize":
                    manager.resize(
                        session_id,
                        int(message.get("columns", 120)),
                        int(message.get("rows", 34)),
                    )
                elif message_type == "ping":
                    await websocket.send_json({"type": "pong"})
        except AISessionNotFound:
            await websocket.close(code=4404, reason="AI session not found")
        except (WebSocketDisconnect, RuntimeError, ValueError):
            return

    def resolve_service_log_target(
        source: str,
        resource_id: str,
    ) -> tuple[DockerRuntime, str, str, int]:
        runtime: DockerRuntime = application.state.runtime
        if source == "deployment":
            deployment = application.state.store.get(resource_id)
            return (
                runtime,
                runtime.container_name(deployment.id),
                deployment.display_name or deployment.model_id,
                deployment.parameters.port,
            )
        if source == "ai-session":
            result = application.state.ai_store.get_result(resource_id)
            return runtime, result.container_name, f"{result.engine} · {result.image}", result.port
        raise LookupError("unsupported log source")

    @application.get("/api/v1/service-logs/{source}/{resource_id}")
    def service_log_snapshot(
        source: str,
        resource_id: str,
        tail: int = Query(default=1000, ge=1, le=5000),
    ) -> dict[str, object]:
        try:
            runtime, container_name, label, port = resolve_service_log_target(source, resource_id)
            return {
                "container_name": container_name,
                "label": label,
                "logs": runtime.logs_for_container(container_name, tail=tail),
                "telemetry": application.state.metrics.inference_telemetry(port),
            }
        except (
            DeploymentNotFound,
            AISessionNotFound,
            AIDeploymentResultNotFound,
            LookupError,
        ) as error:
            raise HTTPException(status_code=404, detail="log source not found") from error
        except DockerCommandError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.get("/api/v1/arena/runs/{run_id}/analysis-context")
    def arena_analysis_context(run_id: str) -> dict[str, object]:
        """Collect bounded, redacted benchmark and inference logs for AI analysis."""
        arena_store: ArenaRunStore = application.state.arena_store
        manager: ArenaManager = application.state.arena_manager
        try:
            run = arena_store.get(run_id)
        except ArenaRunNotFound as error:
            raise HTTPException(status_code=404, detail="Arena run not found") from error

        notes: list[str] = []
        client_log, client_truncated = prepare_log_for_analysis(manager.logs(run_id, tail=2000))
        if not client_log:
            notes.append("客户端 llama-benchy 日志不可用。")
        elif client_truncated:
            notes.append("客户端日志过长，已仅保留最新部分。")

        server_log = ""
        server_telemetry: dict[str, object] = {"available": False}
        source = "deployment" if run.target_type == "deployment" else "ai-session"
        try:
            runtime, container_name, _, port = resolve_service_log_target(source, run.target_id)
            server_log, server_truncated = prepare_log_for_analysis(
                runtime.logs_for_container(container_name, tail=2000)
            )
            server_telemetry = application.state.metrics.inference_telemetry(port)
            if not server_log:
                notes.append("目标推理容器没有返回服务端日志。")
            elif server_truncated:
                notes.append("服务端日志过长，已仅保留最新部分。")
        except (
            DeploymentNotFound,
            AISessionNotFound,
            AIDeploymentResultNotFound,
            DockerCommandError,
            LookupError,
        ) as error:
            safe_error, _ = prepare_log_for_analysis(str(error), max_chars=500)
            notes.append(f"服务端日志不可用：{safe_error}")

        return {
            "benchmark": {
                "id": run.id,
                "status": run.status,
                "target_label": run.target_label,
                "model": run.parameters.report_model_name or run.served_model_name,
                "parameters": run.parameters.model_dump(mode="json"),
                "performance": (
                    run.result_summary.model_dump(mode="json") if run.result_summary else None
                ),
                "speculative_decoding": (
                    run.speculative_metrics.model_dump(mode="json")
                    if run.speculative_metrics
                    else None
                ),
                "resources": (
                    run.resource_summary.model_dump(mode="json") if run.resource_summary else None
                ),
                "leaderboard": (
                    run.leaderboard_reference.model_dump(mode="json")
                    if run.leaderboard_reference
                    else None
                ),
            },
            "client_log": client_log,
            "server_log": server_log,
            "server_telemetry": server_telemetry,
            "log_notes": notes,
        }

    @application.websocket("/api/v1/service-logs/{source}/{resource_id}")
    async def service_log_socket(
        websocket: WebSocket,
        source: str,
        resource_id: str,
    ) -> None:
        await websocket.accept()
        try:
            authentication = await asyncio.wait_for(websocket.receive_json(), timeout=5)
            supplied = str(authentication.get("token", ""))
            if authentication.get("type") != "auth" or (
                resolved_settings.api_token
                and not hmac.compare_digest(supplied, resolved_settings.api_token)
            ):
                await websocket.close(code=4401, reason="invalid API key")
                return
            runtime, container_name, label, port = resolve_service_log_target(source, resource_id)
            await websocket.send_json(
                {
                    "type": "ready",
                    "container_name": container_name,
                    "label": label,
                    "tail": 1000,
                }
            )
            last_error: str | None = None
            while True:
                try:
                    logs = await asyncio.to_thread(
                        runtime.logs_for_container,
                        container_name,
                        tail=1000,
                    )
                    await websocket.send_json(
                        {
                            "type": "snapshot",
                            "logs": logs,
                            "telemetry": application.state.metrics.inference_telemetry(port),
                            "observed_at": time.time(),
                        }
                    )
                    last_error = None
                except DockerCommandError as error:
                    message = str(error)
                    if error.output:
                        message = f"{message}: {error.output.strip()[-1000:]}"
                    if message != last_error:
                        await websocket.send_json({"type": "error", "message": message})
                        last_error = message
                await asyncio.sleep(1)
        except (DeploymentNotFound, AISessionNotFound, AIDeploymentResultNotFound):
            await websocket.close(code=4404, reason="log source not found")
        except (WebSocketDisconnect, RuntimeError, ValueError):
            return

    @application.get("/api/v1/catalog/models", response_model=CatalogModelList)
    def catalog_models(
        request: Request,
        query: str = Query(default="", max_length=255),
        source: str | None = Query(default=None),
        compatibility: str | None = Query(default=None),
        sort: str = Query(
            default="popularity",
            pattern="^(popularity|recent|downloads|likes|name)$",
        ),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> CatalogModelList:
        catalog_store: CatalogStore = request.app.state.catalog_store
        items, total = catalog_store.search_merged(
            query=query,
            source=source,
            compatibility=compatibility,
            sort=sort,
            limit=limit,
            offset=offset,
        )
        return CatalogModelList(items=items, total=total)

    @application.get("/api/v1/catalog/search", response_model=CatalogRemoteSearchRead)
    def search_remote_catalog(
        request: Request,
        query: str = Query(min_length=1, max_length=255),
        source: str = Query(default="all", pattern="^(all|huggingface|modelscope)$"),
        compatibility: str | None = Query(
            default="likely",
            pattern="^(deployable|likely|experimental|incompatible)?$",
        ),
        sort: str = Query(
            default="popularity",
            pattern="^(popularity|recent|downloads|likes|name)$",
        ),
        limit: int = Query(default=25, ge=1, le=50),
    ) -> CatalogRemoteSearchRead:
        service: CatalogService = request.app.state.catalog_service
        store: CatalogStore = request.app.state.catalog_store
        source_items: list[list[CatalogModelRead]] = []
        source_errors: dict[str, str] = {}
        live = False
        if source in {"all", "huggingface"}:
            try:
                source_items.append(service.search_huggingface(query, limit=limit))
                live = True
            except CatalogError as error:
                source_errors["huggingface"] = str(error)
        if source == "all" and not source_errors.get("huggingface"):
            cached_modelscope, _ = store.search(
                query=query,
                source="modelscope",
                compatibility=None,
                limit=limit * 2,
            )
            source_items.append(cached_modelscope)
        if source == "modelscope" or (source == "all" and source_errors.get("huggingface")):
            try:
                source_items.append(service.search_modelscope(query, limit=limit))
                live = True
            except CatalogError as error:
                source_errors["modelscope"] = str(error)
        if compatibility:
            source_items = [
                [
                    item
                    for item in source_group
                    if (
                        item.compatibility != "incompatible"
                        if compatibility == "deployable"
                        else item.compatibility == compatibility
                    )
                ]
                for source_group in source_items
            ]
        if not any(source_items):
            cached, total = store.search_merged(
                query=query,
                source=None if source == "all" else source,
                compatibility=compatibility,
                sort=sort,
                limit=limit * (2 if source == "all" else 1),
            )
            return CatalogRemoteSearchRead(
                items=cached,
                total=total,
                live=False,
                source_errors=source_errors,
            )
        items = merge_catalog_models(
            [item for source_group in source_items for item in source_group],
            sort=sort,
        )[:limit]
        return CatalogRemoteSearchRead(
            items=items,
            total=len(items),
            live=live,
            source_errors=source_errors,
        )

    @application.get(
        "/api/v1/catalog/model-metadata",
        response_model=CatalogModelRead,
    )
    def catalog_model_metadata(
        request: Request,
        source: str = Query(pattern="^(huggingface|modelscope)$"),
        model_id: str = Query(min_length=1, max_length=255),
    ) -> CatalogModelRead:
        service: CatalogService = request.app.state.catalog_service
        try:
            return service.enrich_model_metadata(source, model_id)
        except CatalogError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    @application.get(
        "/api/v1/catalog/publishers/{publisher}/icon",
        include_in_schema=False,
    )
    def catalog_publisher_icon(
        publisher: str,
        request: Request,
        model_id: str | None = Query(default=None, max_length=255),
    ) -> Response:
        service: PublisherIconService = request.app.state.publisher_icons
        content, media_type = service.icon(publisher, model_id)
        return Response(
            content,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @application.get("/api/v1/catalog/refresh/status")
    def catalog_refresh_status(request: Request) -> dict[str, object]:
        refresh_worker: CatalogRefreshWorker = request.app.state.catalog_refresh_worker
        return refresh_worker.snapshot()

    @application.post("/api/v1/catalog/refresh", response_model=CatalogRefreshRead)
    def refresh_catalog(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        source: str = Query(default="all", pattern="^(all|huggingface|modelscope)$"),
    ) -> CatalogRefreshRead:
        service: CatalogService = request.app.state.catalog_service
        requested_sources = (
            ("huggingface", "modelscope") if source == "all" else (source,)
        )
        results: dict[str, dict[str, object]] = {}
        for source_name in requested_sources:
            try:
                results[source_name] = {
                    "ok": True,
                    "refreshed": (
                        service.refresh_modelscope(limit=limit)
                        if source_name == "modelscope"
                        else service.refresh_huggingface(limit=limit)
                    ),
                }
            except CatalogError as error:
                results[source_name] = {
                    "ok": False,
                    "refreshed": 0,
                    "error": str(error),
                }
        if not any(bool(result.get("ok")) for result in results.values()):
            detail = "; ".join(
                f"{name}: {result.get('error', 'refresh failed')}"
                for name, result in results.items()
            )
            raise HTTPException(status_code=502, detail=detail)
        return CatalogRefreshRead(
            source=source,
            refreshed=sum(int(result.get("refreshed", 0)) for result in results.values()),
            sources=results,
            finished_at=datetime.now().astimezone(),
        )

    @application.get("/api/v1/deployments/{deployment_id}/metrics")
    def deployment_metrics(
        deployment_id: str,
        request: Request,
        store: DeploymentStore = Depends(get_store),
    ) -> dict[str, object]:
        try:
            deployment = store.get(deployment_id)
        except DeploymentNotFound as error:
            raise HTTPException(status_code=404, detail="deployment not found") from error
        runtime: DockerRuntime = request.app.state.runtime
        metrics: MetricsService = request.app.state.metrics
        return {
            "container": runtime.stats(deployment_id),
            "vllm": metrics.vllm_metrics(deployment.parameters.port),
        }

    register_ai_accuracy_routes(application, resolved_settings)
    register_dataset_integration_routes(application, resolved_settings)
    register_feishu_routes(application, publication_control_hosts)
    return application


app = create_app()
