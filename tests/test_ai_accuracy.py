from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import tomllib
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from test_accuracy_api import accept_identity, create_target, request_payload, settings_for
from test_accuracy_identity import make_cached_runner

from uniform_dgxspark.accuracy_mcp import AccuracyMCP, tool_definitions
from uniform_dgxspark.ai_store import AISessionStore
from uniform_dgxspark.database import Database
from uniform_dgxspark.main import create_app
from uniform_dgxspark.schemas import AISessionCreate
from uniform_dgxspark.services.ai_terminal import AITerminalManager


def make_idle_cli(path: Path) -> None:
    path.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(300)\n", encoding="utf-8")
    path.chmod(0o700)


@pytest.fixture
def ai_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(
        "uniform_dgxspark.services.docker_runtime.DockerRuntime.discover_ai_services", lambda _: []
    )
    executable, cli = tmp_path / "evalscope", tmp_path / "codex"
    make_cached_runner(executable)
    make_idle_cli(cli)
    app = create_app(
        settings_for(
            tmp_path / "data", executable, codex_binary=str(cli), api_token="platform-test-key"
        )
    )
    with TestClient(app) as client:
        client.headers["X-API-Key"] = "platform-test-key"
        accept_identity(client)
        app.state.ai_accuracy.on_complete = None
        yield client


def new_session(client: TestClient, *, budget: int = 100) -> tuple[str, str, str]:
    target = create_target(client)
    created = client.post(
        "/api/v1/ai/sessions",
        json={
            "provider": "codex",
            "task_type": "accuracy_evaluation",
            "accuracy_target_type": "deployment",
            "accuracy_target_id": target,
            "accuracy_sample_budget": budget,
            "initial_prompt": "测试数学能力",
        },
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    return session_id, client.app.state.ai_terminal._capabilities[session_id], target


def proposal(target: str, *, limit: int = 2) -> dict:
    request = request_payload(target)
    request["datasets"][0]["limit"] = limit
    return {"request": request, "rationale": "先确认数学评测链路；所有成绩均为抽样。"}


def action(
    client: TestClient,
    session_id: str,
    token: str,
    suffix: str,
    payload: dict | None = None,
    *,
    method: str = "POST",
) -> httpx.Response:
    return client.request(
        method,
        f"/api/v1/ai/sessions/{session_id}/actions/accuracy/{suffix}",
        json=payload,
        headers={"X-AI-Capability": token},
    )


def wait_workflow(client: TestClient, session_id: str, statuses: set[str]) -> dict:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        state = client.get(f"/api/v1/ai/sessions/{session_id}/accuracy").json()
        if state["status"] in statuses:
            if not client.app.state.ai_accuracy.is_active(session_id):
                return state
        time.sleep(0.05)
    raise AssertionError(f"AI workflow did not reach {statuses}: {state}")


def test_ai_plan_confirmation_probe_full_canonical_report_and_analysis(ai_site: TestClient) -> None:
    client = ai_site
    session_id, token, target = new_session(client)
    inspected = action(client, session_id, token, "inspect", method="GET")
    assert inspected.status_code == 200 and inspected.json()["environment"]["ready"]
    assert set(inspected.json()["allowed_datasets"]) == {"gsm8k", "arc", "ceval"}
    planned = action(client, session_id, token, "plan", proposal(target, limit=4))
    assert planned.status_code == 200, planned.text
    state = planned.json()
    assert state["status"] == "awaiting_confirmation"
    assert state["proposal"]["probe_samples"] == 2
    assert state["proposal"]["total_samples"] == 6
    assert client.app.state.accuracy_store.list()[1] == 0
    assert action(client, session_id, token, "start").status_code == 409
    assert client.app.state.accuracy_store.list()[1] == 0
    approved = client.post(
        f"/api/v1/ai/sessions/{session_id}/accuracy/confirm",
        json={"proposal_id": state["proposal_id"]},
    )
    assert approved.status_code == 200, approved.text
    complete = wait_workflow(client, session_id, {"completed"})
    assert complete["probe_run"]["summary"]["execution_succeeded"] == 2
    assert complete["full_run"]["summary"]["execution_succeeded"] == 4
    assert complete["full_run"]["metrics"][0]["score"] == 0.5
    assert client.app.state.accuracy_store.list()[1] == 2
    assert action(client, session_id, token, "start").status_code == 200
    assert client.app.state.accuracy_store.list()[1] == 2
    analysis = action(
        client,
        session_id,
        token,
        "analysis",
        {
            "run_id": complete["full_run"]["id"],
            "text": "这是抽样结果。api_key=secret-text",
        },
    )
    assert analysis.status_code == 200
    assert "secret-text" not in analysis.text
    report = client.get(f"/api/v1/ai/sessions/{session_id}/accuracy/report")
    assert report.status_code == 200 and "50.00%" in report.text
    assert "AI 解读" in report.text and "不计算综合平均分" in report.text
    assert ".md" in report.headers["content-disposition"]
    logs = action(client, session_id, token, "logs", method="GET")
    assert logs.status_code == 200
    assert "platform-test-key" not in report.text + logs.text
    assert any(
        event.event_type == "accuracy_plan_confirmed"
        for event in client.app.state.ai_store.list_audit(session_id)
    )


def test_capabilities_cannot_confirm_or_control_other_sessions_or_deploy(
    ai_site: TestClient,
) -> None:
    client = ai_site
    first, token, target = new_session(client)
    second, _, _ = new_session(client)
    state = action(client, first, token, "plan", proposal(target)).json()
    assert action(client, second, token, "status", method="GET").status_code == 403
    assert action(client, first, "bad-token", "plan", proposal(target)).status_code == 403
    assert (
        client.post(
            f"/api/v1/ai/sessions/{first}/accuracy/confirm",
            json={"proposal_id": state["proposal_id"]},
            headers={"X-AI-Capability": token},
        ).status_code
        == 403
    )
    deploy = client.post(
        f"/api/v1/ai/sessions/{first}/actions/deploy", json={}, headers={"X-AI-Capability": token}
    )
    assert deploy.status_code == 403
    client.headers.pop("X-API-Key")
    assert action(client, first, token, "status", method="GET").status_code == 200
    assert (
        client.post(
            f"/api/v1/ai/sessions/{first}/accuracy/confirm",
            json={"proposal_id": state["proposal_id"]},
        ).status_code
        == 401
    )


@pytest.mark.parametrize("change", ["target", "budget", "cache", "dataset"])
def test_invalid_plan_is_rejected_without_model_execution(ai_site: TestClient, change: str) -> None:
    client = ai_site
    session_id, token, target = new_session(client, budget=3)
    payload = proposal(target)
    if change == "target":
        payload["request"]["target_id"] = create_target(client)
    elif change == "budget":
        payload["request"]["datasets"][0]["limit"] = 4
    elif change == "cache":
        payload["request"].update(use_cache=True, cache_source_run_id="other")
    else:
        payload["request"]["datasets"][0]["subset_list"] = ["not-a-subset"]
    rejected = action(client, session_id, token, "plan", payload)
    assert rejected.status_code in {409, 422}, rejected.text
    assert client.app.state.accuracy_store.list()[1] == 0


def test_changed_plan_invalidates_previous_confirmation(ai_site: TestClient) -> None:
    client = ai_site
    session_id, token, target = new_session(client)
    first = action(client, session_id, token, "plan", proposal(target)).json()
    second = action(client, session_id, token, "plan", proposal(target, limit=4)).json()
    assert first["proposal_id"] != second["proposal_id"]
    rejected = client.post(
        f"/api/v1/ai/sessions/{session_id}/accuracy/confirm",
        json={"proposal_id": first["proposal_id"]},
    )
    assert rejected.status_code == 409
    assert client.app.state.accuracy_store.list()[1] == 0


def test_cancelled_probe_never_starts_full_and_preserves_model(ai_site: TestClient) -> None:
    client = ai_site
    session_id, token, target = new_session(client)
    state = action(client, session_id, token, "plan", proposal(target, limit=20)).json()
    assert (
        client.post(
            f"/api/v1/ai/sessions/{session_id}/accuracy/confirm",
            json={"proposal_id": state["proposal_id"]},
        ).status_code
        == 200
    )
    assert client.delete(f"/api/v1/ai/sessions/{session_id}?force=true").status_code == 409
    assert client.post(f"/api/v1/ai/sessions/{session_id}/archive").status_code == 409
    assert client.post(f"/api/v1/ai/sessions/{session_id}/accuracy/cancel").status_code == 200
    state = wait_workflow(client, session_id, {"cancelled"})
    assert state["full_run"] is None
    assert client.app.state.accuracy_store.list()[1] == 1
    assert client.app.state.store.get(target).status.value == "running"


@pytest.mark.parametrize("stop_ai", [False, True])
def test_cleanup_assistant_history_keeps_scores_reports_and_model(
    ai_site: TestClient, stop_ai: bool
) -> None:
    client = ai_site
    session_id, token, target = new_session(client)
    planned = action(client, session_id, token, "plan", proposal(target, limit=4)).json()
    assert client.post(
        f"/api/v1/ai/sessions/{session_id}/accuracy/confirm",
        json={"proposal_id": planned["proposal_id"]},
    ).status_code == 200
    state = wait_workflow(client, session_id, {"completed"})
    runs = [state["probe_run"], state["full_run"]]
    files = {
        path: path.read_bytes()
        for run in runs
        for path in Path(run["run_directory"]).rglob("*")
        if path.is_file()
    }
    assert files
    workspace = Path(client.app.state.ai_store.get(session_id).workspace_path)
    if stop_ai:
        assert client.post(f"/api/v1/ai/sessions/{session_id}/stop").status_code == 200
        assert client.post(f"/api/v1/ai/sessions/{session_id}/archive").status_code == 200
        assert client.get("/api/v1/accuracy/ai/sessions").json()["total"] == 0
        assert client.get(f"/api/v1/ai/sessions/{session_id}/accuracy").json()["proposal_id"]
        assert client.post(f"/api/v1/ai/sessions/{session_id}/restore").status_code == 200
        assert client.get("/api/v1/accuracy/ai/sessions").json()["total"] == 1
    else:
        assert client.post(f"/api/v1/ai/sessions/{session_id}/archive").status_code == 409
        assert client.delete(f"/api/v1/ai/sessions/{session_id}").status_code == 409
    suffix = "" if stop_ai else "?force=true"
    assert client.delete(f"/api/v1/ai/sessions/{session_id}{suffix}").status_code == 204
    assert client.get("/api/v1/accuracy/ai/sessions").json()["total"] == 0
    assert client.get(f"/api/v1/ai/sessions/{session_id}").status_code == 404
    assert not workspace.exists()
    for run in runs:
        kept = client.get(f"/api/v1/accuracy/runs/{run['id']}").json()
        assert kept["metrics"] == run["metrics"]
        assert kept["summary"] == run["summary"]
    assert all(path.is_file() and path.read_bytes() == data for path, data in files.items())
    assert client.app.state.store.get(target).status.value == "running"
    assert not client.app.state.ai_terminal.validate_capability(session_id, token)


def test_failed_probe_blocks_full_and_fabricated_analysis(ai_site: TestClient) -> None:
    client = ai_site
    session_id, token, target = new_session(client)
    state = action(client, session_id, token, "plan", proposal(target, limit=4)).json()
    client.app.state.accuracy_manager.adapter.verify_identity = lambda: (_ for _ in ()).throw(
        ValueError("identity changed")
    )
    assert (
        client.post(
            f"/api/v1/ai/sessions/{session_id}/accuracy/confirm",
            json={"proposal_id": state["proposal_id"]},
        ).status_code
        == 200
    )
    state = wait_workflow(client, session_id, {"blocked"})
    assert state["full_run"] is None
    assert state["probe_run"]["status"] == "failed"
    assert (
        action(
            client,
            session_id,
            token,
            "analysis",
            {
                "run_id": state["probe_run"]["id"],
                "text": "Score is 100%",
            },
        ).status_code
        == 409
    )
    assert client.get(f"/api/v1/ai/sessions/{session_id}/accuracy/report").status_code == 409


def test_resume_keeps_scoped_codex_configuration_and_no_secrets_in_workspace(
    ai_site: TestClient,
) -> None:
    client = ai_site
    session_id, token, _ = new_session(client)
    session = client.app.state.ai_store.get(session_id)
    workspace = Path(session.workspace_path)
    command = AITerminalManager._resume_command("codex", session, workspace, "继续")
    combined = " ".join(command)
    assert "read-only" in combined and "features.shell_tool=false" in combined
    assert "features.unified_exec=false" in combined and "features.apps=false" in combined
    assert "features.multi_agent=false" in combined and "mcp_servers={uniform_accuracy=" in combined
    assert "auto_review" not in combined and "danger-full-access" not in combined
    assert not (workspace / "uniformctl").exists()
    for path in workspace.glob("*.json"):
        assert token not in path.read_text() and "platform-test-key" not in path.read_text()


@pytest.mark.parametrize("resume", [False, True])
def test_codex_approves_only_fixed_accuracy_tools_and_keeps_ui_confirmation(
    tmp_path: Path, resume: bool
) -> None:
    request = AISessionCreate(
        provider="codex",
        task_type="accuracy_evaluation",
        accuracy_target_type="deployment",
        accuracy_target_id="bound-target",
    )
    if resume:
        database = Database(tmp_path / "db.sqlite")
        database.initialize()
        session = AISessionStore(database).create(request, str(tmp_path))
        command = AITerminalManager._resume_command("codex", session, tmp_path, "继续")
    else:
        command = AITerminalManager._command("codex", request, tmp_path)
    overrides = [command[i + 1] for i, arg in enumerate(command[:-1]) if arg == "-c"]
    server_override = next(value for value in overrides if value.startswith("mcp_servers="))
    servers = tomllib.loads(server_override)["mcp_servers"]
    assert set(servers) == {"uniform_accuracy"}
    server = servers["uniform_accuracy"]
    expected = {tool["name"] for tool in tool_definitions()}
    assert len(expected) == 7
    assert set(server["enabled_tools"]) == expected
    assert set(server["tools"]) == expected
    assert all(config == {"approval_mode": "approve"} for config in server["tools"].values())
    # Unknown/future tools stay disabled and cannot inherit a blanket approval.
    assert server["default_tools_approval_mode"] == "prompt"
    assert "accuracy_confirm" not in server["enabled_tools"]
    assert command[command.index("--ask-for-approval") + 1] == "never"
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert 'approvals_reviewer="user"' in overrides
    for feature in ("shell_tool", "unified_exec", "apps", "multi_agent"):
        assert f"features.{feature}=false" in overrides
    assert "auto_review" not in " ".join(command)


def test_accuracy_mcp_read_only_hints_do_not_mislabel_workflow_writes() -> None:
    tools = {tool["name"]: tool for tool in tool_definitions()}
    read_only = {"accuracy_inspect", "accuracy_status", "accuracy_logs"}
    for name, tool in tools.items():
        assert tool["annotations"]["readOnlyHint"] is (name in read_only)


@pytest.mark.parametrize(
    "overrides",
    [
        {"provider": "claude"},
        {"execution_mode": "full_access", "full_access_acknowledged": True},
        {"accuracy_target_id": None},
        {"web_search": True},
        {"deployment_id": "another"},
    ],
)
def test_session_schema_rejects_unscoped_accuracy_modes(overrides: dict) -> None:
    with pytest.raises(ValueError):
        AISessionCreate.model_validate(
            {
                "provider": "codex",
                "task_type": "accuracy_evaluation",
                "accuracy_target_type": "deployment",
                "accuracy_target_id": "target",
                **overrides,
            }
        )


def test_schema_22_migration_preserves_existing_sessions_and_adds_workflow_table(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "db.sqlite")
    database.initialize()
    existing = AISessionStore(database).create(
        AISessionCreate(provider="codex", display_name="Existing deployment helper"),
        str(tmp_path / "old-workspace"),
    )
    with database.connect() as connection:
        connection.execute("DROP TABLE ai_accuracy_workflows")
        connection.execute("DELETE FROM schema_migrations")
        connection.execute("INSERT INTO schema_migrations VALUES(22,'2026-10-22T00:00:00Z')")
        connection.commit()
    database.initialize()
    assert AISessionStore(database).get(existing.id).display_name == "Existing deployment helper"
    with database.connect() as connection:
        assert connection.execute("SELECT * FROM ai_accuracy_workflows").fetchall() == []
        assert [
            row[0]
            for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        ] == [22, 25]


def test_mcp_protocol_tool_scope_error_redaction_and_stdio(tmp_path: Path) -> None:
    server = AccuracyMCP("http://127.0.0.1:18180", "a" * 8, "capability-secret")
    server.client.close()
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(409, json={"detail": "user confirmation required"})

    server.client = httpx.Client(transport=httpx.MockTransport(respond))
    initialized = server.handle(
        {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}
    )
    assert initialized["result"]["protocolVersion"] == "2025-03-26"
    assert server.handle({"method": "notifications/initialized"}) is None
    assert all(
        "confirm" not in tool["name"] and "deploy" not in tool["name"]
        for tool in tool_definitions()
    )
    called = server.handle(
        {"id": 2, "method": "tools/call", "params": {"name": "accuracy_start", "arguments": {}}}
    )
    assert called["result"]["isError"] is True
    assert str(seen[0].url).endswith("/actions/accuracy/start")
    invalid = server.handle(
        {
            "id": 3,
            "method": "tools/call",
            "params": {"name": "accuracy_confirm", "arguments": {"token": "capability-secret"}},
        }
    )
    assert invalid["result"]["isError"] and "capability-secret" not in json.dumps(invalid)
    server.client.close()
    environment = {
        **os.environ,
        "UNIFORM_CONTROL_URL": "http://127.0.0.1:18180",
        "UNIFORM_AI_SESSION_ID": "aaaaaaaa",
        "UNIFORM_AI_CAPABILITY_TOKEN": "secret",
    }
    process = subprocess.run(
        [sys.executable, "-m", "uniform_dgxspark.accuracy_mcp"],
        input='{"id":1,"method":"tools/list"}\n',
        text=True,
        capture_output=True,
        env=environment,
        timeout=10,
    )
    assert process.returncode == 0, process.stderr
    assert len(json.loads(process.stdout)["result"]["tools"]) == 7
    assert "secret" not in process.stdout


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org",
        "http://localhost:18180",
        "http://127.0.0.1:18180/other",
        "http://key@127.0.0.1:18180",
    ],
)
def test_mcp_rejects_non_loopback_or_redirectable_control_urls(url: str) -> None:
    with pytest.raises(ValueError):
        AccuracyMCP(url, "aaaaaaaa", "secret")


def test_deployment_change_invalidates_confirmed_target_identity(ai_site: TestClient) -> None:
    client = ai_site
    session_id, token, target = new_session(client)
    state = action(client, session_id, token, "plan", proposal(target)).json()
    with client.app.state.database.connect() as connection:
        connection.execute("UPDATE deployments SET revision='new-weights' WHERE id=?", (target,))
        connection.commit()
    response = client.post(
        f"/api/v1/ai/sessions/{session_id}/accuracy/confirm",
        json={"proposal_id": state["proposal_id"]},
    )
    assert response.status_code == 409
    assert client.app.state.accuracy_store.list()[1] == 0


def test_restart_recovery_never_automatically_executes_old_approval(ai_site: TestClient) -> None:
    client = ai_site
    session_id, token, target = new_session(client)
    action(client, session_id, token, "plan", proposal(target))
    manager = client.app.state.ai_accuracy
    manager.recover()
    assert manager.get(session_id)["status"] == "awaiting_confirmation"
    manager._update(session_id, status="approved")
    manager.recover()
    state = manager.get(session_id)
    assert state["status"] == "interrupted" and state["proposal"]["request"]["target_id"] == target
    assert action(client, session_id, token, "start").status_code == 409
    assert client.app.state.accuracy_store.list()[1] == 0


def test_status_write_failure_cancels_the_newly_created_evaluation(
    ai_site: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ai_site
    session_id, token, target = new_session(client)
    state = action(client, session_id, token, "plan", proposal(target, limit=20)).json()
    manager = client.app.state.ai_accuracy
    original = manager._update

    def failed_update(session_id: str, **values) -> None:
        if values.get("status") == "probing":
            raise OSError("temporary storage failure")
        original(session_id, **values)

    monkeypatch.setattr(manager, "_update", failed_update)
    result = client.post(
        f"/api/v1/ai/sessions/{session_id}/accuracy/confirm",
        json={"proposal_id": state["proposal_id"]},
    )
    assert result.status_code == 503
    runs, total = client.app.state.accuracy_store.list()
    assert total == 1
    deadline = time.monotonic() + 5
    while client.app.state.accuracy_manager.is_active(runs[0].id):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert client.app.state.accuracy_store.get(runs[0].id).status == "cancelled"


def test_read_only_mode_blocks_plans_and_confirmation_but_keeps_status(
    tmp_path: Path,
) -> None:
    runner, cli = tmp_path / "evalscope", tmp_path / "codex"
    make_cached_runner(runner)
    make_idle_cli(cli)
    with TestClient(
        create_app(
            settings_for(
                tmp_path / "data",
                runner,
                codex_binary=str(cli),
                accuracy_creation_enabled=False,
            )
        )
    ) as client:
        target = create_target(client)
        payload = AISessionCreate(
            provider="codex",
            task_type="accuracy_evaluation",
            accuracy_target_type="deployment",
            accuracy_target_id=target,
        )
        assert client.post("/api/v1/ai/sessions", json=payload.model_dump()).status_code == 403
        # Preserve/read an existing session while creation is disabled.
        session = client.app.state.ai_terminal.create(
            payload,
            deployment_context={
                "accuracy_target": {
                    "target_type": "deployment",
                    "target_id": target,
                    "model_name": "served-model",
                    "label": "model",
                },
                "sample_budget": 100,
            },
        )
        token = client.app.state.ai_terminal._capabilities[session.id]
        assert client.get(f"/api/v1/ai/sessions/{session.id}/accuracy").status_code == 200
        assert action(client, session.id, token, "plan", proposal(target)).status_code == 403
        assert (
            client.post(
                f"/api/v1/ai/sessions/{session.id}/accuracy/confirm", json={"proposal_id": "x"}
            ).status_code
            == 403
        )


def test_mcp_does_not_follow_platform_redirects() -> None:
    server = AccuracyMCP("http://127.0.0.1:18180", "aaaaaaaa", "secret")
    server.client.close()
    seen = []

    def redirect(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://example.org/collect"})

    server.client = httpx.Client(transport=httpx.MockTransport(redirect), follow_redirects=False)
    result = server.handle({"id": 1, "method": "tools/call", "params": {"name": "accuracy_status"}})
    assert result["result"]["isError"] is True
    assert len(seen) == 1
    server.client.close()
