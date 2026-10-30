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
from test_accuracy_api import accept_identity, create_target, settings_for
from test_accuracy_browser import Browser, open_site
from test_accuracy_browser import browser as browser
from test_accuracy_identity import make_cached_runner

from uniform_dgxspark.main import create_app


@pytest.fixture
def ai_browser_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, str]]:
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
    runner, cli = tmp_path / "evalscope", tmp_path / "fake-codex"
    make_cached_runner(runner)
    # A deterministic test double, using the shipped MCP HTTP adapter. No AI/model API calls.
    cli.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, time
from uniform_dgxspark.accuracy_mcp import AccuracyMCP
server=AccuracyMCP(os.environ["UNIFORM_CONTROL_URL"],os.environ["UNIFORM_AI_SESSION_ID"],
                   os.environ["UNIFORM_AI_CAPABILITY_TOKEN"])
def call(name,args=None):
    result=server.call_tool(name,args or {})
    assert not result["isError"],result
    return json.loads(result["content"][0]["text"])
state=call("accuracy_inspect")
target=state["context"]["accuracy_target"]
call("accuracy_propose_plan",{"request":{"target_type":target["target_type"],
     "target_id":target["target_id"],"datasets":[{"name":"gsm8k","subset_list":["main"],
     "limit":4,"few_shot_num":0}]},
     "rationale":"数学小样本计划 <img src=x onerror=window.__aiAccuracyInjected=true>"})
print("FAKE CODEX PROPOSAL READY",flush=True)
deadline=time.monotonic()+30
while time.monotonic()<deadline:
    state=call("accuracy_status")
    if state["status"]=="completed":
        call("accuracy_record_analysis",{"run_id":state["full_run"]["id"],
             "text":"基于真实报告的抽样解读 <script>window.__aiAccuracyInjected=true</script>"})
        break
    if state["status"] in {"cancelled","blocked"}: break
    time.sleep(.1)
server.client.close()
""",
        encoding="utf-8",
    )
    cli.chmod(0o700)
    with TestClient(
        create_app(
            settings_for(
                tmp_path / "data",
                runner,
                codex_binary=str(cli),
                api_token="browser-test-key",
                control_url=url,
            )
        )
    ) as client:
        client.headers["X-API-Key"] = "browser-test-key"
        create_target(client)
        accept_identity(client)
        client.app.state.ai_accuracy.on_complete = None
        server = uvicorn.Server(
            uvicorn.Config(client.app, lifespan="off", log_level="error", ws="none")
        )
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        try:
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.05)
            assert server.started
            yield client, url
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            listener.close()
            assert not thread.is_alive()


def launch(browser: Browser, url: str) -> None:
    open_site(browser, url)
    browser.click('[data-view="accuracy"]')
    browser.wait(
        "document.querySelector('#accuracy-target option[value]:not([value=\"\"])')?.value"
    )
    target = browser.evaluate(
        "document.querySelector('#accuracy-target option[value]:not([value=\"\"])').value"
    )
    browser.fill("#accuracy-target", target)
    browser.click("#open-ai-accuracy")
    browser.fill('#ai-accuracy-form [name="intent"]', "评测数学能力，先用少量样本")
    browser.fill('#ai-accuracy-form [name="sample_budget"]', "10")
    browser.click("#create-ai-accuracy")
    browser.wait("!document.querySelector('#ai-accuracy-dialog').open")
    browser.wait("document.querySelector('.confirm-ai-accuracy')", timeout=20)


def test_browser_ai_plan_confirmation_execution_analysis_and_authenticated_download(
    browser: Browser, ai_browser_site: tuple[TestClient, str], tmp_path: Path
) -> None:
    client, url = ai_browser_site
    launch(browser, url)
    assert client.app.state.accuracy_store.list()[1] == 0
    assert "最多 6 条" in browser.evaluate(
        "document.querySelector('.ai-accuracy-plan').textContent"
    )
    assert browser.evaluate("window.__aiAccuracyInjected||false") is False
    browser.screenshot(tmp_path / "ai-accuracy-plan.png")
    browser.click(".confirm-ai-accuracy")
    browser.wait("document.querySelector('.download-ai-accuracy-report')", timeout=20)
    browser.wait("document.querySelector('.ai-accuracy-analysis')", timeout=20)
    assert browser.evaluate("document.querySelector('.accuracy-score').textContent") == "50.00%"
    assert browser.evaluate("window.__aiAccuracyInjected||false") is False
    assert not browser.evaluate("document.querySelector('.ai-accuracy-analysis script')")
    session_id = client.app.state.ai_store.list(task_type="accuracy_evaluation")[0][0].id
    report = client.get(f"/api/v1/ai/sessions/{session_id}/accuracy/report")
    assert report.status_code == 200 and "50.00%" in report.text
    assert "browser-test-key" not in report.text
    browser.click(".download-ai-accuracy-report")
    browser.screenshot(tmp_path / "ai-accuracy-result.png")
    state = client.get(f"/api/v1/ai/sessions/{session_id}/accuracy").json()
    assert state["probe_run"]["summary"]["execution_succeeded"] == 2
    assert state["full_run"]["summary"]["execution_succeeded"] == 4
    browser.click(".view-ai-accuracy-run")
    browser.wait("document.querySelector('#accuracy-detail-dialog').open")
    browser.wait("document.querySelector('#accuracy-detail-content .accuracy-score')")


def test_browser_ai_mobile_cancel_retains_manual_configuration(
    browser: Browser, ai_browser_site: tuple[TestClient, str]
) -> None:
    client, url = ai_browser_site
    browser.call(
        "Emulation.setDeviceMetricsOverride",
        {
            "width": 390,
            "height": 844,
            "deviceScaleFactor": 1,
            "mobile": False,
        },
    )
    launch(browser, url)
    browser.click(".cancel-ai-accuracy")
    browser.wait("!document.querySelector('.confirm-ai-accuracy')")
    assert client.app.state.accuracy_store.list()[1] == 0
    browser.click("#open-accuracy-config")
    browser.wait("document.querySelector('#accuracy-config-dialog').open")
    assert (
        browser.evaluate("document.querySelector('#accuracy-preset').value") == "quick-objective-v1"
    )
    assert browser.evaluate("document.documentElement.scrollWidth<=window.innerWidth+1")


@pytest.mark.parametrize("mobile", [False, True])
def test_browser_assistant_archive_restore_confirmed_delete_keeps_results(
    browser: Browser, ai_browser_site: tuple[TestClient, str], mobile: bool
) -> None:
    client, url = ai_browser_site
    if mobile:
        browser.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": False},
        )
    launch(browser, url)
    session_id = client.app.state.ai_store.list(task_type="accuracy_evaluation")[0][0].id
    browser.click(".confirm-ai-accuracy")
    browser.wait("document.querySelector('.download-ai-accuracy-report')", timeout=20)
    browser.wait("!document.querySelector('.archive-ai-accuracy').disabled", timeout=20)
    state = client.get(f"/api/v1/ai/sessions/{session_id}/accuracy").json()
    run_ids = [state["probe_run"]["id"], state["full_run"]["id"]]
    assert client.app.state.accuracy_store.list()[1] == 2
    browser.click(".archive-ai-accuracy")
    browser.wait("!document.querySelector('[data-ai-accuracy-session]')")
    assert client.app.state.ai_store.get(session_id).archived_at is not None
    assert client.app.state.accuracy_store.list()[1] == 2
    assert client.post(f"/api/v1/ai/sessions/{session_id}/restore").status_code == 200
    previous_origin = browser.evaluate("performance.timeOrigin")
    browser.call("Page.reload")
    browser.wait(
        f"performance.timeOrigin!=={previous_origin} && document.readyState==='complete'"
    )
    browser.wait("document.querySelector('[data-view=accuracy]')")
    browser.click('[data-view="accuracy"]')
    browser.wait("document.querySelector('.delete-ai-accuracy')")
    # Exercise both confirmation outcomes without blocking Chromium's CDP connection.
    browser.evaluate(
        "window.confirm=message=>{window.__historyDeletePrompt=message;return false;}"
    )
    browser.click(".delete-ai-accuracy")
    assert client.get(f"/api/v1/ai/sessions/{session_id}").status_code == 200
    assert "评测任务、成绩和标准报告会保留" in browser.evaluate("window.__historyDeletePrompt")
    browser.evaluate("window.confirm=()=>true")
    browser.click(".delete-ai-accuracy")
    browser.wait("!document.querySelector('[data-ai-accuracy-session]')")
    assert client.get(f"/api/v1/ai/sessions/{session_id}").status_code == 404
    assert client.app.state.accuracy_store.list()[1] == 2
    for run_id in run_ids:
        assert client.get(f"/api/v1/accuracy/runs/{run_id}").json()["metrics"][0]["score"] == 0.5
    assert browser.evaluate(
        "document.querySelectorAll('#accuracy-run-list .accuracy-run-card').length"
    ) == 2
    assert browser.evaluate(
        "document.documentElement.scrollWidth<=window.innerWidth+1"
    ), browser.evaluate(
        "Array.from(document.querySelectorAll('#view-accuracy *')).filter(el=>"
        "el.getBoundingClientRect().right>window.innerWidth+1).map(el=>"
        "({tag:el.tagName,id:el.id,class:el.className,width:el.getBoundingClientRect().width}))"
        ".slice(0,12)"
    )


def test_browser_running_evaluation_disables_history_cleanup(
    browser: Browser, ai_browser_site: tuple[TestClient, str]
) -> None:
    client, url = ai_browser_site
    launch(browser, url)
    session_id = client.app.state.ai_store.list(task_type="accuracy_evaluation")[0][0].id
    assert browser.evaluate("document.querySelector('.archive-ai-accuracy').disabled")
    assert not browser.evaluate("document.querySelector('.delete-ai-accuracy').disabled")
    client.app.state.ai_accuracy._update(session_id, status="running")
    previous_origin = browser.evaluate("performance.timeOrigin")
    browser.call("Page.reload")
    browser.wait(
        f"performance.timeOrigin!=={previous_origin} && document.readyState==='complete'"
    )
    browser.wait("document.querySelector('[data-view=accuracy]')")
    browser.click('[data-view="accuracy"]')
    browser.wait("document.querySelector('.delete-ai-accuracy')")
    assert browser.evaluate("document.querySelector('.archive-ai-accuracy').disabled")
    assert browser.evaluate("document.querySelector('.delete-ai-accuracy').disabled")
    assert "请先取消" in browser.evaluate("document.querySelector('.delete-ai-accuracy').title")


def test_browser_delete_running_ai_requires_stop_warning_and_confirmation(
    browser: Browser, ai_browser_site: tuple[TestClient, str]
) -> None:
    client, url = ai_browser_site
    launch(browser, url)
    session_id = client.app.state.ai_store.list(task_type="accuracy_evaluation")[0][0].id
    browser.evaluate(
        "window.confirm=message=>{window.__historyDeletePrompt=message;return false;}"
    )
    browser.click(".delete-ai-accuracy")
    assert "AI 对话仍在运行" in browser.evaluate("window.__historyDeletePrompt")
    assert client.get(f"/api/v1/ai/sessions/{session_id}").status_code == 200
    browser.evaluate("window.confirm=()=>true")
    browser.click(".delete-ai-accuracy")
    browser.wait("!document.querySelector('[data-ai-accuracy-session]')")
    assert client.get(f"/api/v1/ai/sessions/{session_id}").status_code == 404
    assert client.app.state.accuracy_store.list()[1] == 0
