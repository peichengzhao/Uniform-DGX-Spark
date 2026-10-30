from __future__ import annotations

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import Mock

from fastapi.testclient import TestClient

from uniform_dgxspark.config import Settings
from uniform_dgxspark.main import create_app
from uniform_dgxspark.schemas import (
    AIDeploymentResultCreate,
    AIDeploymentTemplateCreate,
    AIHealthCheckResult,
    AIInferenceTestResult,
    AISessionCreate,
    DeploymentCreate,
    DeploymentParameters,
)
from uniform_dgxspark.services.deployment_templates import DeploymentTemplateBusy
from uniform_dgxspark.services.docker_runtime import DiscoveredAIService
from uniform_dgxspark.state import DeploymentStatus


class InferenceHandler(BaseHTTPRequestHandler):
    active = 0
    max_active = 0
    requests: list[dict[str, Any]] = []
    delay = 0.04
    lock = threading.Lock()

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path != "/v1/models":
            self.send_error(404)
            return
        self._json({"data": [{"id": "served-test-model"}]})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        size = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(size))
        with self.lock:
            type(self).active += 1
            type(self).max_active = max(type(self).max_active, type(self).active)
            type(self).requests.append(payload)
        time.sleep(type(self).delay)
        with self.lock:
            type(self).active -= 1
        self._json(
            {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {
                    "prompt_tokens": 35,
                    "completion_tokens": 4,
                    "total_tokens": 39,
                },
            }
        )

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _json(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def mark_running(client: TestClient, deployment_id: str) -> None:
    for status in (
        DeploymentStatus.PREFLIGHT,
        DeploymentStatus.DOWNLOADING_MODEL,
        DeploymentStatus.PULLING_IMAGE,
        DeploymentStatus.STARTING_CONTAINER,
        DeploymentStatus.LOADING_MODEL,
        DeploymentStatus.HEALTH_CHECKING,
        DeploymentStatus.RUNNING,
    ):
        client.app.state.store.transition(deployment_id, status, message="test")


def create_ai_owned_service(
    client: TestClient,
    *,
    session_id: str,
    container_name: str,
    port: int,
) -> object:
    workspace = client.app.state.ai_terminal.sessions_dir / session_id
    workspace.mkdir(parents=True)
    session = client.app.state.ai_store.create(
        AISessionCreate(
            provider="codex",
            task_type="deployment",
            display_name="Direct lifecycle service",
            target_model_id="org/direct-model",
            target_source="huggingface",
        ),
        str(workspace),
        session_id=session_id,
    )
    client.app.state.ai_store.upsert_result(
        session.id,
        AIDeploymentResultCreate(
            engine="vllm",
            container_name=container_name,
            container_id="a" * 64,
            image="engine:test",
            port=port,
            service_url=f"http://127.0.0.1:{port}",
            launch_command="docker run engine:test",
            health_check=AIHealthCheckResult(
                url=f"http://127.0.0.1:{port}/health",
                status_code=200,
                passed=True,
            ),
            inference_test=AIInferenceTestResult(
                url=f"http://127.0.0.1:{port}/v1/models",
                status_code=200,
                passed=True,
            ),
        ),
    )
    return session


def register_verified_direct_template(
    client: TestClient,
    *,
    session_id: str,
    container_name: str,
    port: int,
) -> object:
    manager = client.app.state.ai_deployment_templates
    template_dir = manager.templates_dir / session_id
    template_dir.mkdir(parents=True)
    start_script = "#!/bin/bash\nexit 0\n"
    stop_script = "#!/bin/bash\nexit 0\n"
    start_path = template_dir / "start-reusable.sh"
    stop_path = template_dir / "stop-reusable.sh"
    start_path.write_text(start_script, encoding="utf-8")
    stop_path.write_text(stop_script, encoding="utf-8")
    return client.app.state.ai_store.upsert_template(
        AIDeploymentTemplateCreate(
            format_version=manager.FORMAT_VERSION,
            id=session_id,
            source_session_id=session_id,
            model_id="org/direct-model",
            target_source="huggingface",
            deployment_topology="single_node",
            engine="vllm",
            image="engine:test",
            port=port,
            container_name=container_name,
            reuse_mode="direct_script",
            launch_command_template="docker run engine:test",
            script_path=str(start_path),
            script_sha256=hashlib.sha256(start_script.encode()).hexdigest(),
            stop_script_path=str(stop_path),
            stop_script_sha256=hashlib.sha256(stop_script.encode()).hexdigest(),
            container_id="a" * 64,
            artifact_verification_status="verified",
        )
    )


def test_benchmark_runs_concurrent_requests_and_persists_metrics(tmp_path: Path) -> None:
    InferenceHandler.active = 0
    InferenceHandler.max_active = 0
    InferenceHandler.requests = []
    InferenceHandler.delay = 0.04
    server = ThreadingHTTPServer(("127.0.0.1", 0), InferenceHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    app = create_app(Settings(data_dir=tmp_path, worker_enabled=False))
    try:
        with TestClient(app) as client:
            deployment = client.app.state.store.create(
                DeploymentCreate(
                    model_id="org/model",
                    display_name="Benchmark model",
                    parameters=DeploymentParameters(
                        port=server.server_address[1], served_model_name="configured-model"
                    ),
                )
            )
            mark_running(client, deployment.id)
            created = client.post(
                "/api/v1/benchmarks",
                json={
                    "target_type": "deployment",
                    "target_id": deployment.id,
                    "input_tokens": 32,
                    "output_tokens": 4,
                    "concurrency": 2,
                    "total_requests": 4,
                    "warmup_requests": 1,
                    "timeout_seconds": 5,
                },
            )
            assert created.status_code == 201
            benchmark_id = created.json()["id"]

            result: dict[str, Any] = {}
            for _ in range(200):
                result = client.get(f"/api/v1/benchmarks/{benchmark_id}").json()
                if result["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.01)

            assert result["status"] == "completed"
            assert result["completed_requests"] == 4
            assert result["successful_requests"] == 4
            assert result["metrics"]["success_rate"] == 100
            assert result["metrics"]["prompt_tokens"] == 140
            assert result["metrics"]["completion_tokens"] == 16
            assert result["metrics"]["latency_p95_ms"] > 0
            assert result["metrics"]["output_token_throughput"] > 0
            assert InferenceHandler.max_active >= 2
            assert InferenceHandler.requests[0]["model"] == "served-test-model"
            assert len(InferenceHandler.requests) == 5
            listed = client.get("/api/v1/benchmarks").json()
            assert listed["total"] == 1
            assert listed["items"][0]["id"] == benchmark_id
    finally:
        server.shutdown()
        server.server_close()


def test_benchmark_rejects_non_running_deployment(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path, worker_enabled=False))
    with TestClient(app) as client:
        deployment = client.app.state.store.create(DeploymentCreate(model_id="org/not-running"))
        response = client.post(
            "/api/v1/benchmarks",
            json={"target_type": "deployment", "target_id": deployment.id},
        )
        assert response.status_code == 409
        assert response.json()["detail"] == "deployment is not running"


def test_ai_result_benchmark_uses_verified_local_port_and_can_be_cancelled(
    tmp_path: Path,
) -> None:
    InferenceHandler.active = 0
    InferenceHandler.max_active = 0
    InferenceHandler.requests = []
    InferenceHandler.delay = 0.2
    server = ThreadingHTTPServer(("127.0.0.1", 0), InferenceHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    app = create_app(Settings(data_dir=tmp_path / "data", worker_enabled=False))
    try:
        with TestClient(app) as client:
            workspace = tmp_path / "data" / "ai-sessions" / "result-session"
            workspace.mkdir(parents=True)
            session = client.app.state.ai_store.create(
                AISessionCreate(
                    provider="codex",
                    target_model_id="org/ai-model",
                    target_source="huggingface",
                ),
                str(workspace),
                session_id="result-session",
                display_name="AI model deployment",
            )
            client.app.state.ai_store.update_status(session.id, "exited", exit_code=0)
            client.app.state.ai_store.upsert_result(
                session.id,
                AIDeploymentResultCreate(
                    engine="vLLM",
                    container_name="model-server",
                    image="engine:test",
                    port=server.server_address[1],
                    service_url="http://untrusted.example:9999",
                    launch_command="docker run engine:test",
                    health_check=AIHealthCheckResult(
                        url="http://127.0.0.1/health", status_code=200, passed=True
                    ),
                    inference_test=AIInferenceTestResult(
                        url="http://127.0.0.1/v1/chat/completions",
                        status_code=200,
                        passed=True,
                    ),
                ),
            )

            created = client.post(
                "/api/v1/benchmarks",
                json={
                    "target_type": "ai_session",
                    "target_id": session.id,
                    "total_requests": 20,
                    "concurrency": 1,
                    "warmup_requests": 0,
                    "timeout_seconds": 5,
                },
            )
            assert created.status_code == 201
            assert created.json()["service_url"] == (
                f"http://127.0.0.1:{server.server_address[1]}"
            )
            cancelled = client.post(
                f"/api/v1/benchmarks/{created.json()['id']}/cancel"
            )
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"
    finally:
        server.shutdown()
        server.server_close()


def test_ai_autonomous_result_is_listed_as_live_service(tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), InferenceHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    app = create_app(Settings(data_dir=tmp_path / "data", worker_enabled=False))
    try:
        with TestClient(app) as client:
            workspace = tmp_path / "data" / "ai-sessions" / "visible-session"
            workspace.mkdir(parents=True)
            session = client.app.state.ai_store.create(
                AISessionCreate(
                    provider="codex",
                    display_name="Visible AI service",
                    target_model_id="org/visible-model",
                    target_source="huggingface",
                ),
                str(workspace),
                session_id="visible-session",
            )
            client.app.state.ai_store.upsert_result(
                session.id,
                AIDeploymentResultCreate(
                    engine="vLLM",
                    container_name="visible-server",
                    image="engine:test",
                    port=server.server_address[1],
                    service_url="http://127.0.0.1:1",
                    launch_command="docker run engine:test",
                    health_check=AIHealthCheckResult(
                        url="http://127.0.0.1/health", status_code=200, passed=True
                    ),
                    inference_test=AIInferenceTestResult(
                        url="http://127.0.0.1/v1/chat/completions",
                        status_code=200,
                        passed=True,
                    ),
                ),
            )
            running = {"value": True}
            client.app.state.runtime.is_named_container_running = (
                lambda _name: running["value"]
            )
            client.app.state.runtime.discover_ai_services = lambda: []
            health_timeouts: list[float] = []
            original_health = client.app.state.runtime.health

            def observed_health(port: int, *, timeout: float = 2) -> bool:
                health_timeouts.append(timeout)
                return original_health(port, timeout=timeout)

            client.app.state.runtime.health = observed_health

            response = client.get("/api/v1/ai/services")
            assert response.status_code == 200
            service = response.json()["items"][0]
            assert service["session_id"] == session.id
            assert service["display_name"] == "Visible AI service"
            assert service["runtime_status"] == "running"
            assert service["ai_session_status"] == "starting"
            assert service["health_reachable"] is True
            assert service["linked_deployment_id"] is None
            assert service["launch_command"] == "docker run engine:test"
            assert service["service_url"] == "http://127.0.0.1:1"
            assert health_timeouts == [3.0]
            send_instruction = Mock(return_value=session)
            client.app.state.ai_terminal.send_instruction = send_instruction
            requested = client.post(f"/api/v1/ai/services/{session.id}/stop")
            assert requested.status_code == 202
            assert requested.json()["status"] == "original_session"
            prompt = send_instruction.call_args.args[1]
            assert "停止服务" in prompt
            assert "visible-server" in prompt
            assert "不要停止 Uniform 平台" in prompt
            audit = client.get(f"/api/v1/ai/sessions/{session.id}/audit").json()["items"]
            assert audit[-1]["event_type"] == "service_stop_requested"
            running["value"] = False
            client.app.state.arena_manager._inspect_container_config = lambda name: (
                "bash /start.sh",
                {
                    "NNODES": "2",
                    "TP": "2",
                    "MAX_MODEL_LEN": "65536",
                    "GPU_MEMORY_UTILIZATION": "0.82",
                    "QUANTIZATION": "awq",
                    "SPEC_METHOD": "draft",
                },
                True,
            )
            profile = client.get(
                "/api/v1/arena/runtime-profile",
                params={"target_type": "ai_session", "target_id": session.id},
            )
            assert profile.status_code == 200
            assert profile.json()["configuration_source"] == "当前容器配置"
            assert profile.json()["cluster_size"] == 2
            assert profile.json()["tensor_parallel"] == 2
            assert profile.json()["max_model_len"] == 65536
            assert profile.json()["gpu_memory_utilization"] == 0.82
            assert profile.json()["quantization"] == "awq"
            assert profile.json()["speculative"]["enabled"] is True

            client.app.state.runtime.is_named_container_running = lambda _name: False
            stale = client.get("/api/v1/ai/services").json()["items"][0]
            assert stale["runtime_status"] == "stopped"
            assert stale["container_running"] is False
            assert stale["health_reachable"] is False
            already_stopped = client.post(f"/api/v1/ai/services/{session.id}/stop")
            assert already_stopped.status_code == 409
            assert already_stopped.json()["detail"] == "model service is not running"
    finally:
        server.shutdown()
        server.server_close()


def test_ai_service_stop_uses_verified_direct_script_without_ai(
    client: TestClient,
) -> None:
    session = create_ai_owned_service(
        client,
        session_id="direct-stop-session",
        container_name="direct-stop-container",
        port=18892,
    )
    template = register_verified_direct_template(
        client,
        session_id=session.id,
        container_name="direct-stop-container",
        port=18892,
    )
    running = {"value": True}
    runtime = client.app.state.runtime
    runtime.is_named_container_running = lambda _name: running["value"]
    runtime.health = lambda _port, timeout=2: running["value"]
    runtime.named_container_identity = Mock(
        return_value={
            "container_id": "a" * 64,
            "image": "engine:test",
            "running": True,
            "auto_remove": False,
        }
    )
    template_manager = client.app.state.ai_deployment_templates

    def execute_direct_stop(_template: object, action: str) -> str:
        assert action == "stop"
        running["value"] = False
        return "container stopped"

    template_manager.execute = Mock(side_effect=execute_direct_stop)
    ai_instruction = Mock()
    client.app.state.ai_terminal.send_instruction = ai_instruction

    requested = client.post(f"/api/v1/ai/services/{session.id}/stop")

    assert requested.status_code == 202
    assert requested.json() == {
        "status": "completed",
        "session_id": session.id,
        "stop_session_id": None,
    }
    template_manager.execute.assert_called_once_with(template, "stop")
    ai_instruction.assert_not_called()
    audit_types = {
        item["event_type"]
        for item in client.get(f"/api/v1/ai/sessions/{session.id}/audit").json()[
            "items"
        ]
    }
    assert "service_stopped_directly" in audit_types


def test_ai_service_stop_direct_failure_hands_off_to_original_ai(
    client: TestClient,
) -> None:
    session = create_ai_owned_service(
        client,
        session_id="failed-direct-stop-session",
        container_name="failed-direct-stop-container",
        port=18893,
    )
    template = register_verified_direct_template(
        client,
        session_id=session.id,
        container_name="failed-direct-stop-container",
        port=18893,
    )
    running = {"value": True}
    runtime = client.app.state.runtime
    runtime.is_named_container_running = lambda _name: running["value"]
    runtime.health = lambda _port, timeout=2: running["value"]
    runtime.named_container_identity = Mock(
        return_value={
            "container_id": "a" * 64,
            "image": "engine:test",
            "running": True,
            "auto_remove": False,
        }
    )
    template_manager = client.app.state.ai_deployment_templates
    template_manager.execute = Mock(side_effect=RuntimeError("docker stop failed"))

    def stop_from_original_ai(_session_id: str, _prompt: str) -> object:
        running["value"] = False
        return session

    ai_instruction = Mock(side_effect=stop_from_original_ai)
    client.app.state.ai_terminal.send_instruction = ai_instruction

    requested = client.post(f"/api/v1/ai/services/{session.id}/stop")

    assert requested.status_code == 202
    assert requested.json() == {
        "status": "original_session",
        "session_id": session.id,
        "stop_session_id": session.id,
    }
    template_manager.execute.assert_called_once_with(template, "stop")
    ai_instruction.assert_called_once()
    assert ai_instruction.call_args.args[0] == session.id
    assert "直接停止脚本失败" in ai_instruction.call_args.args[1]
    assert "docker stop failed" in ai_instruction.call_args.args[1]
    downgraded = client.app.state.ai_store.get_template(session.id)
    assert downgraded.reuse_mode == "ai_session"
    assert downgraded.artifact_verification_status == "failed"
    assert "docker stop failed" in (downgraded.artifact_verification_error or "")
    audit_types = {
        item["event_type"]
        for item in client.get(f"/api/v1/ai/sessions/{session.id}/audit").json()[
            "items"
        ]
    }
    assert "service_stop_direct_failed" in audit_types


def test_ai_service_stop_busy_returns_retry_without_downgrading_or_ai(
    client: TestClient,
) -> None:
    session = create_ai_owned_service(
        client,
        session_id="busy-direct-stop-session",
        container_name="busy-direct-stop-container",
        port=18894,
    )
    register_verified_direct_template(
        client,
        session_id=session.id,
        container_name="busy-direct-stop-container",
        port=18894,
    )
    runtime = client.app.state.runtime
    runtime.is_named_container_running = lambda _name: True
    runtime.health = lambda _port, timeout=2: True
    template_manager = client.app.state.ai_deployment_templates
    template_manager.operation = Mock(
        side_effect=DeploymentTemplateBusy("lifecycle operation is busy")
    )
    template_manager.update_verification = Mock()
    ai_instruction = Mock()
    client.app.state.ai_terminal.send_instruction = ai_instruction
    revoke = Mock()
    client.app.state.publication_manager.revoke = revoke

    requested = client.post(f"/api/v1/ai/services/{session.id}/stop")

    assert requested.status_code == 409
    assert requested.headers["retry-after"] == "1"
    assert "busy" in requested.json()["detail"]
    assert client.app.state.ai_store.get_template(session.id).reuse_mode == "direct_script"
    template_manager.update_verification.assert_not_called()
    ai_instruction.assert_not_called()
    revoke.assert_not_called()
    service = client.get("/api/v1/ai/services").json()["items"][0]
    assert service["stop_status"] == "failed"


def test_ai_service_stop_snapshot_failure_keeps_publication_and_is_retryable(
    client: TestClient,
) -> None:
    session = create_ai_owned_service(
        client,
        session_id="snapshot-failed-stop-session",
        container_name="snapshot-failed-stop-container",
        port=18895,
    )
    runtime = client.app.state.runtime
    runtime.is_named_container_running = lambda _name: True
    runtime.health = lambda _port, timeout=2: True
    backup = Mock(side_effect=OSError("snapshot disk is unavailable"))
    client.app.state.ai_terminal.backup_service_context = backup
    ai_instruction = Mock()
    client.app.state.ai_terminal.send_instruction = ai_instruction
    revoke = Mock()
    client.app.state.publication_manager.revoke = revoke

    requested = client.post(f"/api/v1/ai/services/{session.id}/stop")

    assert requested.status_code == 502
    assert "snapshot disk is unavailable" in requested.json()["detail"]
    revoke.assert_not_called()
    ai_instruction.assert_not_called()
    service = client.get("/api/v1/ai/services").json()["items"][0]
    assert service["stop_status"] == "idle"


def test_multi_node_stop_waits_for_ai_completion_after_main_service_closes(
    client: TestClient,
) -> None:
    session = create_ai_owned_service(
        client,
        session_id="multi-node-stop-session",
        container_name="multi-node-stop-container",
        port=18896,
    )
    workspace = Path(session.workspace_path)
    (workspace / "deployment-context.json").write_text(
        json.dumps({"deployment_topology": {"resolved": "multi_node"}}),
        encoding="utf-8",
    )
    running = {"value": True}
    runtime = client.app.state.runtime
    runtime.is_named_container_running = lambda _name: running["value"]
    runtime.health = lambda _port, timeout=2: running["value"]

    def close_main_service(_session_id: str, _prompt: str) -> object:
        running["value"] = False
        return session

    client.app.state.ai_terminal.send_instruction = Mock(side_effect=close_main_service)

    requested = client.post(f"/api/v1/ai/services/{session.id}/stop")

    assert requested.status_code == 202
    time.sleep(0.05)
    stopping = client.get("/api/v1/ai/services").json()["items"][0]
    assert stopping["stop_status"] == "original_session"

    client.app.state.ai_store.update_status(session.id, "exited", exit_code=0)
    for _ in range(200):
        stopped = client.get("/api/v1/ai/services").json()["items"][0]
        if stopped["stop_status"] == "completed":
            break
        time.sleep(0.01)
    assert stopped["stop_status"] == "completed"
    assert stopped["stop_message"] == "主服务已关闭，AI 已完成远端停服流程"


def test_ai_service_list_recovers_a_missing_registration(client: TestClient) -> None:
    session_id = "recovered-session-1234"
    workspace = client.app.state.ai_terminal.sessions_dir / session_id
    workspace.mkdir(parents=True)
    runtime = client.app.state.runtime
    runtime.discover_ai_services = lambda: [
        DiscoveredAIService(
            session_id=session_id,
            display_name="Recovered Qwen",
            container_name="uniform-qwen-r0",
            container_id="sha256:recovered",
            image="engine:test",
            engine="vllm",
            port=18888,
            model_name="fallback-model",
            launch_command="vllm serve /model --port 18888",
        )
    ]
    runtime.openai_models = lambda _port, timeout=2: ["served-qwen"]
    runtime.health = lambda _port, timeout=2: True
    runtime.is_named_container_running = lambda _name: True

    response = client.get("/api/v1/ai/services")

    assert response.status_code == 200
    service = response.json()["items"][0]
    assert service["session_id"] == session_id
    assert service["display_name"] == "Recovered Qwen"
    assert service["runtime_status"] == "running"
    assert service["port"] == 18888
    session = client.app.state.ai_store.get(session_id)
    assert session.target_model_id == "served-qwen"
    assert session.status == "exited"
    assert client.app.state.ai_store.get_result(session_id).container_name == "uniform-qwen-r0"


def test_stopped_ai_service_archive_hides_and_restores_related_session(
    client: TestClient,
) -> None:
    workspace = client.app.state.ai_terminal.sessions_dir / "archive-service-session"
    workspace.mkdir(parents=True)
    session = client.app.state.ai_store.create(
        AISessionCreate(
            provider="codex",
            task_type="deployment",
            display_name="Archived service",
        ),
        str(workspace),
        session_id="archive-service-session",
    )
    session = client.app.state.ai_store.update_status(session.id, "exited")
    client.app.state.ai_store.upsert_result(
        session.id,
        AIDeploymentResultCreate(
            engine="vllm",
            container_name="archived-service-container",
            image="engine:test",
            port=18891,
            service_url="http://127.0.0.1:18891",
            launch_command="docker run engine:test",
            health_check=AIHealthCheckResult(
                url="http://127.0.0.1:18891/health", status_code=200, passed=True
            ),
            inference_test=AIInferenceTestResult(
                url="http://127.0.0.1:18891/v1/models", status_code=200, passed=True
            ),
        ),
    )
    running = {"value": False}
    client.app.state.runtime.is_named_container_running = (
        lambda _name: running["value"]
    )

    archived = client.post(f"/api/v1/ai/services/{session.id}/archive")
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None
    assert client.get("/api/v1/ai/services").json() == {"items": [], "total": 0}
    assert client.get("/api/v1/ai/sessions").json()["total"] == 0

    archived_services = client.get(
        "/api/v1/ai/services", params={"include_archived": True}
    ).json()
    assert archived_services["total"] == 1
    assert archived_services["items"][0]["archived_at"] is not None

    restored = client.post(f"/api/v1/ai/services/{session.id}/restore")
    assert restored.status_code == 200
    assert restored.json()["archived_at"] is None
    assert client.get("/api/v1/ai/services").json()["total"] == 1
    assert client.get("/api/v1/ai/sessions").json()["total"] == 1

    running["value"] = True
    blocked = client.post(f"/api/v1/ai/services/{session.id}/archive")
    assert blocked.status_code == 409
    generic_blocked = client.post(f"/api/v1/ai/sessions/{session.id}/archive")
    assert generic_blocked.status_code == 409


def test_service_stop_falls_back_to_new_ai_session_with_durable_snapshot(
    client: TestClient,
) -> None:
    workspace = client.app.state.ai_terminal.sessions_dir / "stop-source-session"
    workspace.mkdir(parents=True)
    (workspace / "deployment-context.json").write_text(
        '{"service":"context"}', encoding="utf-8"
    )
    session = client.app.state.ai_store.create(
        AISessionCreate(provider="codex", task_type="deployment", display_name="Stop source"),
        str(workspace),
        session_id="stop-source-session",
    )
    client.app.state.ai_store.upsert_result(
        session.id,
        AIDeploymentResultCreate(
            engine="vllm",
            container_name="stop-source-container",
            image="engine:test",
            port=18889,
            service_url="http://127.0.0.1:18889",
            launch_command="docker run engine:test",
            health_check=AIHealthCheckResult(
                url="http://127.0.0.1:18889/health", status_code=200, passed=True
            ),
            inference_test=AIInferenceTestResult(
                url="http://127.0.0.1:18889/v1/models", status_code=200, passed=True
            ),
        ),
    )
    running = {"value": True}
    runtime = client.app.state.runtime
    runtime.is_named_container_running = lambda _name: running["value"]
    runtime.health = lambda _port, timeout=2: running["value"]
    manager = client.app.state.ai_terminal
    manager.send_instruction = Mock(side_effect=RuntimeError("original context unavailable"))

    def create_fallback(*args: object, **kwargs: object) -> object:
        del args, kwargs
        running["value"] = False
        return session.model_copy(
            update={"id": "fallback-stop-session", "task_type": "service_stop"}
        )

    manager.create = Mock(side_effect=create_fallback)

    requested = client.post(f"/api/v1/ai/services/{session.id}/stop")
    assert requested.status_code == 202
    assert requested.json()["status"] == "fallback_session"
    for _ in range(100):
        service = client.get("/api/v1/ai/services").json()["items"][0]
        if service["stop_status"] == "completed":
            break
        time.sleep(0.01)
    assert service["stop_status"] == "completed"
    assert service["stop_session_id"] == "fallback-stop-session"
    request = manager.create.call_args.args[0]
    assert request.task_type == "service_stop"
    snapshot = (
        client.app.state.ai_terminal.sessions_dir.parent
        / "ai-service-snapshots"
        / session.id
        / "SERVICE_HANDOFF.json"
    )
    assert snapshot.is_file()
    assert "deployment-context.json" in snapshot.read_text(encoding="utf-8")
    manager.delete(session.id, force=True)
    assert not workspace.exists()
    assert snapshot.is_file()
