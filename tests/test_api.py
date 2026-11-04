from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

from fastapi.testclient import TestClient

from uniform_dgxspark.config import Settings
from uniform_dgxspark.main import create_app
from uniform_dgxspark.services.docker_runtime import CommandResult
from uniform_dgxspark.state import DeploymentStatus


def test_startup_backfill_keeps_newest_active_deployment_template(tmp_path: Path) -> None:
    from uniform_dgxspark.ai_store import AISessionStore
    from uniform_dgxspark.database import Database
    from uniform_dgxspark.schemas import (
        AIDeploymentResultCreate,
        AIDeploymentTemplateCreate,
        AIHealthCheckResult,
        AIInferenceTestResult,
        AISessionCreate,
    )

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database = Database(data_dir / "uniform.db")
    database.initialize()
    store = AISessionStore(database)

    def create_result(session_id: str, *, port: int) -> None:
        workspace = data_dir / "ai-sessions" / session_id
        workspace.mkdir(parents=True)
        session = store.create(
            AISessionCreate(
                provider="codex",
                task_type="deployment",
                target_model_id="org/shared-model",
                target_source="huggingface",
            ),
            str(workspace),
            session_id=session_id,
        )
        (workspace / "deployment-context.json").write_text(
            json.dumps(
                {
                    "deployment": {
                        "target_model": {
                            "model_id": "org/shared-model",
                            "source": "huggingface",
                        }
                    },
                    "deployment_topology": {"resolved": "single_node"},
                }
            ),
            encoding="utf-8",
        )
        store.upsert_result(
            session.id,
            AIDeploymentResultCreate(
                engine="sglang",
                container_name=f"container-{session_id}",
                image="engine:test",
                port=port,
                service_url=f"http://127.0.0.1:{port}",
                launch_command=f"serve --port {port}",
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

    create_result("archived-old", port=18880)
    store.archive("archived-old")
    create_result("active-new", port=18881)
    current_script = data_dir / "current-start.sh"
    current_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    store.upsert_template(
        AIDeploymentTemplateCreate(
            format_version=3,
            id="active-new",
            source_session_id="active-new",
            model_id="org/shared-model",
            target_source="huggingface",
            deployment_topology="single_node",
            engine="sglang",
            image="engine:test",
            port=18881,
            container_name="container-active-new",
            reuse_mode="ai_session",
            launch_command_template="serve --port 18881",
            script_path=str(current_script),
            script_sha256="0" * 64,
        )
    )

    app = create_app(Settings(data_dir=data_dir, worker_enabled=False, codex_binary="/bin/true"))
    with TestClient(app) as client:
        latest = client.get(
            "/api/v1/ai/deployment-templates/latest",
            params={
                "model_id": "org/shared-model",
                "target_source": "huggingface",
                "deployment_topology": "single_node",
            },
        )

    assert latest.status_code == 200
    assert latest.json()["id"] == "active-new"


class ServiceLogRunner:
    def run(self, arguments: list[str], *, timeout: int = 120) -> CommandResult:
        del timeout
        assert arguments[1:5] == ["logs", "--timestamps", "--tail", "1000"]
        return CommandResult(
            0,
            '2026-09-01T12:00:00Z INFO "POST /v1/chat/completions HTTP/1.1" 200 OK\n',
            "",
        )


def test_health(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "database": "ok",
        "recovered_tasks": 0,
    }

    index = client.get("/")
    assert index.status_code == 200
    assert index.headers["cache-control"] == "no-store"
    assert "Uniform" in index.text
    assert 'name="display_name"' in index.text
    assert 'name="ai_model"' in index.text
    assert 'name="reasoning_effort"' in index.text
    assert 'data-view="catalog">模型中心' in index.text
    assert 'data-view="arena">性能评测</button>' in index.text
    assert 'data-view="accuracy">精度评测</button>' in index.text
    assert 'data-view="ai-sessions">AI 会话</button>' in index.text
    assert 'data-view="local-models"' not in index.text
    assert 'data-view="images"' not in index.text
    assert 'data-view="ai"' not in index.text
    assert 'id="model-panel-local"' in index.text
    assert 'id="image-panel"' in index.text
    assert 'id="view-ai-sessions"' in index.text
    assert 'id="view-accuracy"' in index.text
    assert 'id="ai-session-workspace"' in index.text
    assert 'id="performance-panel-ai"' not in index.text
    assert 'data-view="benchmarks"' not in index.text
    assert 'data-benchmark-method="arena"' in index.text
    assert 'data-benchmark-method="custom"' in index.text
    assert "自定义性能测试" in index.text
    assert 'id="ai-benchmark-list"' in index.text
    assert 'id="resume-ai-session"' in index.text
    assert 'id="auto-resume-notice"' in index.text
    assert 'data-ai-session-category="deployment"' in index.text
    assert 'data-ai-session-category="benchmark"' in index.text
    assert 'data-ai-session-category="stop"' in index.text
    assert 'id="ui-edit-toggle"' in index.text
    assert 'id="ui-edit-dialog"' in index.text
    assert 'id="ui-edit-screenshot"' in index.text
    assert 'id="ui-screenshot-dialog"' in index.text
    assert 'id="ui-edit-history-dialog"' in index.text
    assert 'id="ui-edit-history-list"' in index.text
    assert 'id="ui-edit-history-retry"' in index.text
    assert 'id="service-publication-dialog"' in index.text
    assert 'id="ai-multi-template-dialog"' in index.text
    assert 'id="reuse-multi-node-template"' in index.text
    assert 'id="create-single-node-ai-deploy"' in index.text
    assert 'id="detail-eyebrow"' in index.text
    assert 'src="/static/app.js?v=' in index.text

    stylesheet = client.get("/static/app.css")
    assert stylesheet.status_code == 200

    application = client.get("/static/app.js")
    assert application.status_code == 200
    assert application.headers["cache-control"] == "no-store"
    assert "当前状态每 3 秒重新探测" in application.text
    assert "首次验收通过" in application.text
    assert "canBenchmark" in application.text
    assert "uiEditTargetContext" in application.text
    assert "full_image_data_url" in application.text
    assert "annotated_image_data_url" in application.text
    assert "html2canvas" in application.text
    assert "getDisplayMedia" not in application.text
    assert "chromiumCaptureCurrentPage" in application.text
    assert "reloadAfterUiEdit" in application.text
    assert "benchmark-result?publish=false" in application.text
    assert "stop-ai-service" in application.text
    assert "/api/v1/ai/services/${sessionId}/stop" in application.text
    assert "openAiSessionConversation" in application.text
    assert "ai-session-history" in application.text
    assert "autoResumeCountdown" in application.text
    assert "AI 请求受限，已进入自动续跑队列" in application.text
    assert "Prefill 总计/均值" in application.text
    assert "/api/v1/ai/deployment-templates/latest" in application.text
    assert "/api/v1/ai/deployment-templates/launch" in application.text
    assert "client_request_id:requestId" in application.text
    assert "detailRuntimeMetricsMarkup" in application.text
    assert "detailBenchmarkMarkup" in application.text
    assert "fetchDeploymentLocalMetadata" in application.text
    assert "detail-advanced" in application.text
    assert "/api/v1/benchmarks?limit=200" in application.text
    assert "<h3>执行时间线</h3>" not in application.text
    assert "局域网访问" in application.text
    assert "/api/v1/service-publications/" in application.text
    assert (
        'latestDeploymentTemplate(model,targetSource,localPath,"single_node")' in application.text
    )
    assert 'latestDeploymentTemplate(model,targetSource,localPath,"multi_node")' in application.text
    assert "deployment_topology:topology" in application.text

    client.app.state.ui_page_capture.capture = Mock(return_value=b"\x89PNG\r\n\x1a\nmock")
    capture = client.post(
        "/api/v1/ui-editor/capture",
        json={
            "html": "<html><head></head><body>" + "page " * 20 + "</body></html>",
            "viewport_width": 1280,
            "viewport_height": 720,
            "device_pixel_ratio": 1,
            "scroll_x": 0,
            "scroll_y": 120,
        },
    )
    assert capture.status_code == 200
    assert capture.content.startswith(b"\x89PNG")
    assert capture.headers["x-uniform-capture-engine"] == "chromium"

    ui_editor = client.get("/api/v1/ui-editor/status")
    assert ui_editor.status_code == 200
    assert ui_editor.json()["enabled"] is True
    assert isinstance(ui_editor.json()["provider_available"], bool)

    ui_edits = client.get("/api/v1/ui-edits")
    assert ui_edits.status_code == 200
    assert ui_edits.json() == {"items": [], "total": 0}

    missing_ui_edit = client.get("/api/v1/ui-edits/does-not-exist")
    assert missing_ui_edit.status_code == 404
    full_cli_log = client.get("/api/v1/ui-edits/does-not-exist/logs?tail_bytes=1000000")
    assert full_cli_log.status_code == 404

    terminal_library = client.get("/static/vendor/node_modules/@xterm/xterm/lib/xterm.mjs")
    assert terminal_library.status_code == 200
    screenshot_library = client.get(
        "/static/vendor/node_modules/html2canvas/dist/html2canvas.esm.js"
    )
    assert screenshot_library.status_code == 200

    metrics = client.get("/api/v1/system/metrics")
    assert metrics.status_code == 200
    assert "unified_memory" in metrics.json()
    assert "gpu" in metrics.json()


def test_managed_deployment_service_log_websocket(client: TestClient) -> None:
    deployment = client.post(
        "/api/v1/deployments",
        json={"model_id": "org/log-model"},
    ).json()
    client.app.state.runtime.runner = ServiceLogRunner()

    with client.websocket_connect(
        f"/api/v1/service-logs/deployment/{deployment['id']}"
    ) as websocket:
        websocket.send_json({"type": "auth", "token": ""})
        ready = websocket.receive_json()
        snapshot = websocket.receive_json()

    assert ready["type"] == "ready"
    assert ready["container_name"].startswith("uniform-vllm-")
    assert snapshot["type"] == "snapshot"
    assert "POST /v1/chat/completions" in snapshot["logs"]

    rest_snapshot = client.get(f"/api/v1/service-logs/deployment/{deployment['id']}?tail=1000")
    assert rest_snapshot.status_code == 200
    assert "POST /v1/chat/completions" in rest_snapshot.json()["logs"]


def test_create_list_get_cancel_and_events(client: TestClient) -> None:
    create_response = client.post(
        "/api/v1/deployments",
        json={
            "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
            "revision": "test-revision",
            "display_name": "qwen-test",
            "parameters": {
                "port": 18080,
                "max_model_len": 2048,
                "gpu_memory_utilization": 0.8,
            },
        },
    )

    assert create_response.status_code == 201
    created = create_response.json()
    assert created["status"] == "queued"
    assert created["progress"] == 0
    assert created["attempt"] == 0

    deployment_id = created["id"]
    get_response = client.get(f"/api/v1/deployments/{deployment_id}")
    assert get_response.status_code == 200
    assert get_response.json()["model_id"] == "Qwen/Qwen2.5-0.5B-Instruct"

    list_response = client.get("/api/v1/deployments")
    assert list_response.status_code == 200
    assert list_response.json()["total"] == 1
    assert list_response.json()["items"][0]["id"] == deployment_id

    cancel_response = client.post(f"/api/v1/deployments/{deployment_id}/cancel")
    assert cancel_response.status_code == 200
    assert cancel_response.json()["status"] == "cancelled"

    second_cancel = client.post(f"/api/v1/deployments/{deployment_id}/cancel")
    assert second_cancel.status_code == 200
    assert second_cancel.json()["status"] == "cancelled"

    events_response = client.get(f"/api/v1/deployments/{deployment_id}/events")
    assert events_response.status_code == 200
    events = events_response.json()["items"]
    assert [event["event_type"] for event in events] == ["created", "cancelled"]
    assert [event["sequence"] for event in events] == [1, 2]


def test_stopped_deployment_can_be_archived_and_restored(client: TestClient) -> None:
    created = client.post("/api/v1/deployments", json={"model_id": "org/archive-model"}).json()
    deployment_id = created["id"]

    active_archive = client.post(f"/api/v1/deployments/{deployment_id}/archive")
    assert active_archive.status_code == 409

    client.post(f"/api/v1/deployments/{deployment_id}/cancel")
    client.app.state.runtime.is_running = lambda _deployment_id: False
    archived = client.post(f"/api/v1/deployments/{deployment_id}/archive")
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None
    assert client.get("/api/v1/deployments").json() == {"items": [], "total": 0}

    archived_list = client.get("/api/v1/deployments", params={"include_archived": True}).json()
    assert archived_list["total"] == 1
    assert archived_list["items"][0]["id"] == deployment_id

    restored = client.post(f"/api/v1/deployments/{deployment_id}/restore")
    assert restored.status_code == 200
    assert restored.json()["archived_at"] is None
    assert client.get("/api/v1/deployments").json()["total"] == 1
    event_types = [
        item["event_type"]
        for item in client.get(f"/api/v1/deployments/{deployment_id}/events").json()["items"]
    ]
    assert event_types[-2:] == ["archived", "restored"]


def test_running_deployment_is_rechecked_against_container_and_health(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/v1/deployments",
        json={"model_id": "org/live-check", "parameters": {"port": 18082}},
    ).json()
    store = client.app.state.store
    for deployment_status in (
        DeploymentStatus.PREFLIGHT,
        DeploymentStatus.DOWNLOADING_MODEL,
        DeploymentStatus.PULLING_IMAGE,
        DeploymentStatus.STARTING_CONTAINER,
        DeploymentStatus.LOADING_MODEL,
        DeploymentStatus.HEALTH_CHECKING,
        DeploymentStatus.RUNNING,
    ):
        store.transition(created["id"], deployment_status, message="test")

    runtime = client.app.state.runtime
    runtime.is_running = lambda _deployment_id: False
    stopped = client.get("/api/v1/deployments").json()["items"][0]
    assert stopped["status"] == "stopped"
    assert stopped["error_code"] == "runtime_container_missing"

    runtime.is_running = lambda _deployment_id: True
    runtime.health = lambda _port, timeout=2: False
    unhealthy = client.get(f"/api/v1/deployments/{created['id']}").json()
    assert unhealthy["status"] == "failed"
    assert unhealthy["error_code"] == "runtime_health_unreachable"

    runtime.health = lambda _port, timeout=2: True
    running = client.get("/api/v1/deployments").json()["items"][0]
    assert running["status"] == "running"


def test_validation_and_not_found(client: TestClient) -> None:
    invalid = client.post(
        "/api/v1/deployments",
        json={"model_id": "invalid model id"},
    )
    assert invalid.status_code == 422

    missing = client.get("/api/v1/deployments/does-not-exist")
    assert missing.status_code == 404
