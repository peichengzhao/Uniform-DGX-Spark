from __future__ import annotations

import socket
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient
from test_accuracy_api import create_target, settings_for
from test_accuracy_identity import make_cached_runner, wait_terminal

from uniform_dgxspark.database import SCHEMA_VERSION, Database
from uniform_dgxspark.dataset_integration_mcp import DatasetIntegrationMCP
from uniform_dgxspark.main import create_app
from uniform_dgxspark.schemas import AccuracyRunCreate, AISessionCreate
from uniform_dgxspark.services.ai_terminal import AITerminalManager


@pytest.fixture
def integration_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, str, str]]:
    for method in ("discover_ai_services", "list_images"):
        monkeypatch.setattr(
            f"uniform_dgxspark.services.docker_runtime.DockerRuntime.{method}", lambda _: []
        )
    monkeypatch.setattr(
        "uniform_dgxspark.services.docker_runtime.DockerRuntime.is_running", lambda *_: True
    )
    monkeypatch.setattr(
        "uniform_dgxspark.services.docker_runtime.DockerRuntime.health", lambda *_, **__: True
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    runner, cli = tmp_path / "fake-evalscope", tmp_path / "fake-codex"
    make_cached_runner(runner)
    runner.write_text(
        runner.read_text().replace(
            "args=sys.argv[1:]",
            "args=sys.argv[1:]\nif '--version' in args:\n print('evalscope 0.0.0_dev');sys.exit(0)",
        )
    )
    cli.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, time, threading, sys
from uniform_dgxspark.dataset_integration_mcp import DatasetIntegrationMCP
server=DatasetIntegrationMCP(os.environ['UNIFORM_CONTROL_URL'],os.environ['UNIFORM_AI_SESSION_ID'],
                            os.environ['UNIFORM_AI_CAPABILITY_TOKEN'])
def call(name,args=None):
 result=server.call_tool(name,args or {})
 assert not result['isError'],result
 return json.loads(result['content'][0]['text'])
state=call('dataset_inspect')
if state['can_onboard'] and state['environment']['ready']:
 call('dataset_propose_plan',{'subset':state['dataset']['subsets'][0],
     'rationale':'检查固定版本的原生客观判分 '
                 '<img src=x onerror=window.__integrationInjected=true>'})
 print('FAKE CODEX: 方案已提交，等待平台确认',flush=True)
else:
 call('dataset_record_analysis',{'text':'需要专用适配与执行安全验证，当前尚未接入。'})
 print('FAKE CODEX: 该数据集尚缺专用适配',flush=True)
def read_input():
 for line in sys.stdin: print('收到补充需求：'+line.strip(),flush=True)
threading.Thread(target=read_input,daemon=True).start()
deadline=time.monotonic()+60
while time.monotonic()<deadline:
 state=call('dataset_status')
 if state['status']=='ready':
  call('dataset_record_analysis',{'text':'已基于真实两条验证报告接入；小样本不代表完整基准成绩。'})
  print('FAKE CODEX: 已验证接入',flush=True)
  break
 if state['status']=='cancelled':break
 time.sleep(.1)
server.client.close()
""",
        encoding="utf-8",
    )
    cli.chmod(0o700)
    app = create_app(
        settings_for(
            tmp_path / "data",
            runner,
            codex_binary=str(cli),
            api_token="browser-test-key",
            control_url=url,
            accuracy_evalscope_source=Path("/home/dgx-spark/Downloads/evalscope"),
        )
    )
    with TestClient(app) as client:
        client.headers["X-API-Key"] = "browser-test-key"
        target = create_target(client)
        server = uvicorn.Server(
            uvicorn.Config(app, lifespan="off", log_level="error", ws="websockets-sansio")
        )
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.05)
            assert server.started
            yield client, url, target
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            listener.close()
            assert not thread.is_alive()


def start_integration(client: TestClient, target: str, name: str = "mmlu") -> str:
    response = client.post(
        "/api/v1/ai/sessions",
        json={
            "provider": "codex",
            "task_type": "dataset_integration",
            "execution_mode": "interactive",
            "accuracy_dataset_name": name,
            "accuracy_target_type": "deployment",
            "accuracy_target_id": target,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def integration_state(client: TestClient, session: str, status: str) -> dict:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/ai/sessions/{session}/datasets")
        assert response.status_code == 200, response.text
        result = response.json()
        if result["status"] == status:
            return result
        time.sleep(0.05)
    raise AssertionError(f"integration did not reach {status}: {result}")


def test_real_probe_gates_activation_and_persists_without_session(
    integration_site: tuple[TestClient, str, str],
) -> None:
    client, _, target = integration_site
    session = start_integration(client, target)
    proposal = integration_state(client, session, "proposed")
    assert client.app.state.accuracy_store.list()[1] == 0
    assert "mmlu" not in client.app.state.accuracy_manager.adapter.dataset_catalog()
    request = proposal["proposal"]["request"]
    assert client.post("/api/v1/accuracy/runs", json=request).status_code == 422
    token = client.app.state.ai_terminal._capabilities[session]
    base = f"/api/v1/ai/sessions/{session}"
    assert (
        client.post(
            base + "/datasets/confirm",
            headers={"X-AI-Capability": token},
            json={"proposal_id": proposal["proposal_id"]},
        ).status_code
        == 403
    )
    assert (
        client.post(
            base + "/actions/deploy", headers={"X-AI-Capability": token}, json={}
        ).status_code
        == 403
    )
    assert client.post(base + "/datasets/confirm", json={"proposal_id": "stale"}).status_code == 409
    confirmed = client.post(
        base + "/datasets/confirm", json={"proposal_id": proposal["proposal_id"]}
    )
    assert confirmed.status_code == 200, confirmed.text
    repeated = client.post(
        base + "/datasets/confirm", json={"proposal_id": proposal["proposal_id"]}
    )
    assert repeated.status_code == 200
    result = integration_state(client, session, "ready")
    assert result["integrated"] and result["validation_run"]["summary"]["execution_succeeded"] == 2
    assert client.app.state.accuracy_store.list()[1] == 1
    catalog = client.get("/api/v1/accuracy/datasets/catalog").json()
    assert len(catalog["allowed_datasets"]["mmlu"]["subsets"]) == 57
    assert catalog["allowed_datasets"]["mmlu"]["zero_shot_only"]
    model = AccuracyRunCreate.model_validate(request)
    resolved = client.app.state.accuracy_manager.adapter.resolve_config(model, {})
    assert resolved.datasets[0].name == "mmlu"
    manual = client.post("/api/v1/accuracy/runs", json=request)
    assert manual.status_code == 201, manual.text
    assert wait_terminal(client, manual.json()["id"])["status"] == "completed"
    assert client.post(base + "/datasets/cancel").json()["status"] == "ready"
    # Archive/delete only the AI conversation; activated contracts and reports are durable.
    deadline = time.monotonic() + 5
    while client.app.state.dataset_integrations.is_active(session) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert client.delete(base + "?force=true").status_code == 204
    assert "mmlu" in client.app.state.accuracy_manager.adapter.dataset_catalog()
    assert client.app.state.accuracy_store.list()[1] == 2
    from uniform_dgxspark.services.dataset_integration import DatasetIntegrationManager

    rebuilt = DatasetIntegrationManager(
        client.app.state.database, client.app.state.accuracy_manager, client.app.state.ai_terminal
    )
    assert "mmlu" in rebuilt.approved_specs()


def test_unsupported_dataset_explains_requirements_without_execution(
    integration_site: tuple[TestClient, str, str],
) -> None:
    client, _, target = integration_site
    session = start_integration(client, target, "live_code_bench")
    state = integration_state(client, session, "blocked")
    assert not state["integrated"]
    assert "专用适配" in state["error"]
    token = client.app.state.ai_terminal._capabilities[session]
    path = f"/api/v1/ai/sessions/{session}/actions/datasets"
    response = client.post(
        path + "/plan",
        headers={"X-AI-Capability": token},
        json={"subset": "default", "rationale": "override"},
    )
    assert response.status_code == 409
    assert client.app.state.accuracy_store.list()[1] == 0
    assert "live_code_bench" not in client.app.state.accuracy_manager.adapter.dataset_catalog()


def test_binding_and_confirm_contracts_reject_cross_scope_and_mutation(
    integration_site: tuple[TestClient, str, str],
) -> None:
    client, _, target = integration_site
    first = start_integration(client, target, "boolq")
    second = start_integration(client, target, "mmlu")
    integration_state(client, first, "proposed")
    integration_state(client, second, "proposed")
    token = client.app.state.ai_terminal._capabilities[first]
    assert (
        client.get(
            f"/api/v1/ai/sessions/{second}/actions/datasets/status",
            headers={"X-AI-Capability": token},
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"/api/v1/ai/sessions/{first}/actions/accuracy/inspect",
            headers={"X-AI-Capability": token},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/ai/sessions/{first}/actions/datasets/plan",
            headers={"X-AI-Capability": token},
            json={"subset": "default", "rationale": "plan", "dataset_name": "mmlu"},
        ).status_code
        == 422
    )
    assert client.app.state.accuracy_store.list()[1] == 0


def test_new_codex_config_exposes_only_bound_dataset_tools_and_no_shell() -> None:
    import tomllib

    config = AITerminalManager._dataset_codex_config()
    settings = {}
    for i, value in enumerate(config):
        if value == "-c":
            for key, entry in tomllib.loads(config[i + 1]).items():
                if isinstance(entry, dict) and isinstance(settings.get(key), dict):
                    settings[key].update(entry)
                else:
                    settings[key] = entry
    server = settings["mcp_servers"]["uniform_dataset"]
    assert set(server["enabled_tools"]) == {t["name"] for t in DatasetIntegrationMCP.definitions()}
    assert all(v["approval_mode"] == "approve" for v in server["tools"].values())
    assert server["default_tools_approval_mode"] == "prompt"
    assert config[config.index("--sandbox") + 1] == "read-only"
    assert settings["features"]["shell_tool"] is False
    assert settings["approvals_reviewer"] == "user"
    request = AISessionCreate(
        provider="codex",
        task_type="dataset_integration",
        execution_mode="interactive",
        accuracy_dataset_name="mmlu",
    )
    command = AITerminalManager._command("codex", request, Path("/tmp/test-workspace"))
    assert "dataset_inspect first" in command[-1]
    assert "uniform_accuracy" not in "\n".join(command)


def test_schema_23_upgrade_retains_existing_accuracy_tables(tmp_path: Path) -> None:
    database = Database(tmp_path / "uniform.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute("DROP TABLE ai_dataset_integrations")
        connection.execute("DROP TABLE accuracy_dataset_integrations")
        connection.execute("DELETE FROM schema_migrations")
        connection.execute("INSERT INTO schema_migrations VALUES(23,'2026-10-27')")
        connection.commit()
    database.initialize()
    with database.connect() as connection:
        assert [
            r[0]
            for r in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        ] == [23, SCHEMA_VERSION]
        assert connection.execute("SELECT * FROM accuracy_dataset_integrations").fetchall() == []
        assert connection.execute("SELECT * FROM accuracy_runs").fetchall() == []


@pytest.mark.parametrize(
    "name", ["aime24", "aime25", "aime26", "amc", "mmlu", "hellaswag", "boolq", "sciq"]
)
def test_audited_candidates_have_native_contracts_but_are_not_activated(
    integration_site: tuple[TestClient, str, str],
    name: str,
) -> None:
    client, _, _ = integration_site
    before = set(sys.modules)
    spec, fingerprint = client.app.state.dataset_integrations.candidate(name)
    assert spec.zero_shot_only and spec.subsets and len(fingerprint) == 64
    assert spec.dataset_id and spec.evaluation_version
    assert name not in client.app.state.accuracy_manager.adapter.dataset_catalog()
    assert not any(
        module == "evalscope" or module.startswith("evalscope.")
        for module in set(sys.modules) - before
    )
    assert client.app.state.accuracy_store.list()[1] == 0


@pytest.mark.parametrize("failure", ["invalid_report", "source_changed", "target_changed"])
def test_failed_or_stale_validation_cannot_activate(
    integration_site: tuple[TestClient, str, str],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from uniform_dgxspark.services.evalscope_accuracy import EvalScopeAccuracyError

    client, _, target = integration_site
    session = start_integration(client, target, "boolq")
    proposal = integration_state(client, session, "proposed")
    if failure == "invalid_report":
        executable = client.app.state.accuracy_manager.adapter.evalscope_command
        executable.write_text(
            executable.read_text().replace("'schema_version':2", "'schema_version':3")
        )
    elif failure == "source_changed":

        def deny_source():
            raise EvalScopeAccuracyError("test source contract changed")

        monkeypatch.setattr(client.app.state.dataset_integrations, "verify_source", deny_source)
    else:
        from uniform_dgxspark.state import DeploymentStatus

        client.app.state.store.transition(
            target, DeploymentStatus.STOPPING, message="test-only stop"
        )
        client.app.state.store.transition(
            target, DeploymentStatus.STOPPED, message="test-only stop"
        )
    response = client.post(
        f"/api/v1/ai/sessions/{session}/datasets/confirm",
        json={"proposal_id": proposal["proposal_id"]},
    )
    if failure == "invalid_report":
        assert response.status_code == 200, response.text
        result = integration_state(client, session, "blocked")
        assert result["validation_run"]["status"] == "failed"
    else:
        assert response.status_code in {409, 422}
        assert client.app.state.accuracy_store.list()[1] == 0
    assert "boolq" not in client.app.state.accuracy_manager.adapter.dataset_catalog()


def test_zero_accuracy_still_counts_as_valid_integration_not_a_quality_gate(
    integration_site: tuple[TestClient, str, str],
) -> None:
    client, _, target = integration_site
    executable = client.app.state.accuracy_manager.adapter.evalscope_command
    executable.write_text(executable.read_text().replace("'score':0.5", "'score':0.0"))
    session = start_integration(client, target, "boolq")
    proposal = integration_state(client, session, "proposed")
    response = client.post(
        f"/api/v1/ai/sessions/{session}/datasets/confirm",
        json={"proposal_id": proposal["proposal_id"]},
    )
    assert response.status_code == 200, response.text
    result = integration_state(client, session, "ready")
    assert all(metric["score"] == 0 for metric in result["validation_run"]["metrics"])
    assert result["integrated"]


def test_cancelled_probe_is_not_activated_and_cannot_be_deleted_while_active(
    integration_site: tuple[TestClient, str, str],
) -> None:
    client, _, target = integration_site
    executable = client.app.state.accuracy_manager.adapter.evalscope_command
    executable.write_text(executable.read_text().replace("time.sleep(0.15)", "time.sleep(1.2)"))
    session = start_integration(client, target, "boolq")
    proposal = integration_state(client, session, "proposed")
    base = f"/api/v1/ai/sessions/{session}"
    confirmed = client.post(
        base + "/datasets/confirm", json={"proposal_id": proposal["proposal_id"]}
    )
    assert confirmed.status_code == 200, confirmed.text
    assert client.delete(base + "?force=true").status_code == 409
    assert client.post(base + "/archive").status_code == 409
    cancelled = client.post(base + "/datasets/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert wait_terminal(client, confirmed.json()["validation_run_id"])["status"] == "cancelled"
    assert "boolq" not in client.app.state.accuracy_manager.adapter.dataset_catalog()
    assert client.app.state.store.get(target).status.value == "running"
