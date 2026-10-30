from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from test_accuracy_api import accept_identity, create_target, make_fake_evalscope, settings_for
from test_accuracy_identity import make_cached_runner, wait_terminal
from websockets.sync.client import connect

from uniform_dgxspark.main import create_app


class Browser:
    """Exercise the shipped page through Chromium's debugging protocol."""

    def __init__(self, url: str) -> None:
        self.connection = connect(url, proxy=None)
        self.socket = self.connection.__enter__()
        self.sequence = 0
        self.exceptions: list[dict[str, Any]] = []
        self.call("Runtime.enable")
        self.call("Page.enable")

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.sequence += 1
        self.socket.send(
            json.dumps({"id": self.sequence, "method": method, "params": params or {}})
        )
        while True:
            message = json.loads(self.socket.recv(timeout=15))
            if message.get("method") == "Runtime.exceptionThrown":
                self.exceptions.append(message["params"])
            if message.get("id") == self.sequence:
                assert "error" not in message, message
                return message.get("result", {})

    def evaluate(self, expression: str) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        assert "exceptionDetails" not in result, result
        return result.get("result", {}).get("value")

    def wait(self, expression: str, *, timeout: float = 15) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.evaluate(f"Boolean({expression})")
            if value:
                return value
            time.sleep(0.1)
        raise AssertionError(f"Browser condition did not become true: {expression}")

    def click(self, selector: str) -> None:
        position = self.evaluate(
            f"(() => {{ const el=document.querySelector({json.dumps(selector)});"
            "if(!el||el.disabled)throw new Error('Control unavailable');"
            "el.scrollIntoView({block:'center'});const r=el.getBoundingClientRect();"
            "const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);"
            "if(!el.contains(hit))throw new Error('Control covered by '+hit?.outerHTML);"
            "return {x:r.x+r.width/2,y:r.y+r.height/2}; })()"
        )
        self.call(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "button": "left", "clickCount": 1, **position},
        )
        self.call(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "button": "left", "clickCount": 1, **position},
        )

    def fill(self, selector: str, value: str) -> None:
        self.evaluate(
            f"(() => {{ const el=document.querySelector({json.dumps(selector)});"
            f"el.value={json.dumps(value)};"
            "el.dispatchEvent(new Event('input',{bubbles:true}));"
            "el.dispatchEvent(new Event('change',{bubbles:true})); })()"
        )

    def screenshot(self, path: Path) -> None:
        path.write_bytes(base64.b64decode(self.call("Page.captureScreenshot")["data"]))


@pytest.fixture
def browser(tmp_path: Path) -> Iterator[Browser]:
    candidates = [
        os.getenv("UNIFORM_TEST_CHROMIUM", ""),
        "/snap/chromium/current/usr/lib/chromium-browser/chrome",
        shutil.which("chromium") or "",
        shutil.which("google-chrome") or "",
    ]
    executable = next((path for path in candidates if path and Path(path).is_file()), None)
    if executable is None:
        if os.getenv("UNIFORM_REQUIRE_BROWSER") == "1":
            pytest.fail("Chromium is required for stage 40 browser acceptance")
        pytest.skip("Chromium is not installed")
    profile = tmp_path / "browser-profile"
    with (tmp_path / "chromium.log").open("w") as log:
        process = subprocess.Popen(
            [
                executable,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-background-networking",
                "--no-first-run",
                "--remote-debugging-port=0",
                f"--user-data-dir={profile}",
                "about:blank",
            ],
            env={
                **os.environ,
                "XDG_CONFIG_HOME": str(tmp_path / "browser-config"),
                "XDG_CACHE_HOME": str(tmp_path / "browser-cache"),
            },
            stdout=log,
            stderr=log,
        )
        connection = None
        try:
            active_port = profile / "DevToolsActivePort"
            deadline = time.monotonic() + 15
            while not active_port.exists() and time.monotonic() < deadline:
                assert process.poll() is None, (tmp_path / "chromium.log").read_text()
                time.sleep(0.1)
            assert active_port.exists(), "Chromium did not expose a debug port"
            port = active_port.read_text().splitlines()[0]
            with httpx.Client(trust_env=False) as client:
                pages = client.get(f"http://127.0.0.1:{port}/json").json()
            connection = Browser(
                next(page["webSocketDebuggerUrl"] for page in pages if page["type"] == "page")
            )
            connection.call(
                "Emulation.setDeviceMetricsOverride",
                {"width": 1440, "height": 1000, "deviceScaleFactor": 1, "mobile": False},
            )
            yield connection
            assert not connection.exceptions, connection.exceptions
        finally:
            if connection is not None:
                connection.connection.__exit__(None, None, None)
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@pytest.fixture
def accuracy_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, str]]:
    # Keep the browser away from the developer's Docker services and real model resources.
    monkeypatch.setattr(
        "uniform_dgxspark.services.docker_runtime.DockerRuntime.discover_ai_services", lambda _: []
    )
    monkeypatch.setattr(
        "uniform_dgxspark.services.docker_runtime.DockerRuntime.list_images", lambda _: []
    )
    monkeypatch.setattr(
        "uniform_dgxspark.services.docker_runtime.DockerRuntime.is_running", lambda *_: True
    )
    monkeypatch.setattr(
        "uniform_dgxspark.services.docker_runtime.DockerRuntime.health", lambda *_, **__: True
    )
    executable = tmp_path / "fake-evalscope"
    make_fake_evalscope(executable, delay_seconds=6)
    app = create_app(settings_for(tmp_path / "data", executable, api_token="browser-test-key"))
    with TestClient(app) as client:
        create_target(client)
        accept_identity(client)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error", ws="none"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.05)
            assert server.started
            yield client, f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            listener.close()
            assert not thread.is_alive()


def open_site(browser: Browser, url: str) -> None:
    browser.call(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "sessionStorage.setItem('uniform_api_token','browser-test-key');"},
    )
    browser.call("Page.navigate", {"url": url})
    browser.wait(
        "document.querySelector('.accuracy-service') && "
        "!document.querySelector('.accuracy-service').disabled"
    )


def test_accuracy_browser_presets_validation_and_mobile_layout(
    browser: Browser, accuracy_site: tuple[TestClient, str], tmp_path: Path
) -> None:
    _client, url = accuracy_site
    open_site(browser, url)
    browser.click(".accuracy-service")
    browser.wait("document.querySelector('#accuracy-config-dialog').open")
    assert browser.evaluate(
        "document.querySelector('#accuracy-form-target').value.startsWith('deployment|')"
    )
    browser.fill('[name="temperature"]', "0.75")
    browser.fill("#accuracy-preset", "balanced-objective-v1")
    assert browser.evaluate("document.querySelector('[name=temperature]').value") == "0"
    assert browser.evaluate(
        "[...document.querySelectorAll('[data-accuracy-dataset]>label>input')].every(el=>el.checked)"
    )
    browser.fill("#accuracy-preset", "formal-objective-v1")
    assert browser.evaluate("document.querySelector('[name=gsm8k_limit]').value") == "1000"
    assert (
        browser.evaluate("document.querySelector('[name=ceval_subsets]').value.split(',').length")
        == 10
    )
    browser.fill("#accuracy-preset", "quick-objective-v1")
    browser.fill('[name="max_tokens"]', "0")
    assert not browser.evaluate("document.querySelector('#accuracy-form').checkValidity()")
    assert browser.evaluate("document.querySelector('.accuracy-advanced').open")
    browser.fill('[name="max_tokens"]', "2048")
    browser.fill('[name="gsm8k_limit"]', "")
    assert not browser.evaluate("document.querySelector('#accuracy-form').checkValidity()")
    browser.fill('[name="gsm8k_limit"]', "2")
    browser.click('[name="dataset_gsm8k"]')
    browser.click("#start-accuracy")
    browser.wait("document.querySelector('#accuracy-form-error').textContent.includes('至少启用')")
    browser.click('[name="dataset_ceval"]')
    browser.fill('[name="ceval_subsets"]', "unknown_subject")
    browser.click("#start-accuracy")
    browser.wait(
        "document.querySelector('#accuracy-form-error').textContent"
        ".includes('unsupported ceval subsets')"
    )
    browser.fill("#accuracy-preset", "balanced-objective-v1")
    browser.call(
        "Emulation.setDeviceMetricsOverride",
        {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True},
    )
    assert browser.evaluate("document.documentElement.scrollWidth <= innerWidth")
    browser.screenshot(tmp_path / "accuracy-mobile.png")
    assert browser.evaluate(
        "(() => { const button=document.querySelector('#start-accuracy');"
        "button.scrollIntoView({block:'center'});const r=button.getBoundingClientRect();"
        "return document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)===button; })()"
    )
    browser.click("#accuracy-config-dialog [data-accuracy-config-close]")
    browser.call(
        "Emulation.setDeviceMetricsOverride",
        {"width": 1440, "height": 1000, "deviceScaleFactor": 1, "mobile": False},
    )
    browser.screenshot(tmp_path / "accuracy-desktop.png")


def test_accuracy_browser_task_results_logs_download_cancel_and_delete(
    browser: Browser, accuracy_site: tuple[TestClient, str], tmp_path: Path
) -> None:
    client, url = accuracy_site
    open_site(browser, url)
    browser.click(".accuracy-service")
    browser.fill('[name="gsm8k_limit"]', "2")
    browser.click("#start-accuracy")
    browser.wait("document.querySelector('#accuracy-run-list .status.running')")
    browser.wait("document.querySelector('#open-accuracy-config').disabled")
    browser.click(".accuracy-detail")
    browser.wait(
        "document.querySelector('#accuracy-detail-logs')?.textContent.includes('<redacted>')"
    )
    assert "bearer-secret" not in browser.evaluate(
        "document.querySelector('#accuracy-detail-logs').textContent"
    )
    browser.wait("document.querySelector('#accuracy-run-list .status.completed')")
    browser.wait("document.querySelector('#accuracy-detail-status').textContent.includes('已完成')")
    assert (
        browser.evaluate(
            "document.querySelector('#accuracy-detail-content .accuracy-score').textContent"
        )
        == "100.00%"
    )
    completed = next(
        run for run in client.app.state.accuracy_store.list()[0] if run.status == "completed"
    )
    log_path = next((Path(completed.run_directory) / "evalscope").glob("*/evalscope.log"))
    with log_path.open("a") as log:
        log.write('\n<img src="bad" onerror="window.accuracyInjected=true">\n')
    assert "<img" in client.app.state.accuracy_manager.logs(completed.id).logs
    browser.click("#accuracy-detail-dialog [data-accuracy-detail-close]")
    browser.click(".accuracy-detail")
    try:
        browser.wait(
            "document.querySelector('#accuracy-detail-logs')?.textContent.includes('<img')"
        )
    except AssertionError:
        browser.screenshot(tmp_path / "accuracy-reopen-failure.png")
        raise AssertionError(
            browser.evaluate(
                "({dialogs:[...document.querySelectorAll('dialog[open]')].map(el=>el.id),"
                "logs:document.querySelector('#accuracy-detail-logs')?.textContent,"
                "content:document.querySelector('#accuracy-detail-content')?.textContent})"
            )
        ) from None
    assert browser.evaluate("document.querySelectorAll('#accuracy-detail-logs img').length === 0")
    assert not browser.evaluate("Boolean(window.accuracyInjected)")
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    browser.call(
        "Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(download_dir)}
    )
    browser.click('.download-accuracy-artifact[data-path="uniform-accuracy-config.json"]')
    deadline = time.monotonic() + 5
    files = []
    while time.monotonic() < deadline:
        files = list(download_dir.glob("*.json"))
        if files:
            break
        time.sleep(0.1)
    assert files
    assert json.loads(files[0].read_text())["model_name"] == "served-model"
    browser.screenshot(tmp_path / "accuracy-results.png")
    browser.click("#accuracy-detail-dialog [data-accuracy-detail-close]")
    browser.click(".rerun-accuracy")
    assert browser.evaluate("document.querySelector('[name=gsm8k_limit]').value") == "2"
    browser.click("#start-accuracy")
    browser.wait("document.querySelector('#accuracy-run-list .status.running')")
    browser.click(".cancel-accuracy")
    browser.wait("document.querySelector('#accuracy-run-list .status.cancelled')")
    cancelled = next(
        run for run in client.app.state.accuracy_store.list()[0] if run.status == "cancelled"
    )
    deadline = time.monotonic() + 5
    while client.app.state.accuracy_manager.is_active(cancelled.id) and time.monotonic() < deadline:
        time.sleep(0.05)
    browser.evaluate("window.confirm=()=>true")
    browser.click(".delete-accuracy")
    browser.wait("document.querySelectorAll('.accuracy-run-card').length === 1")
    assert not Path(cancelled.run_directory).exists()


def test_accuracy_browser_shows_disabled_feature(
    browser: Browser, accuracy_site: tuple[TestClient, str]
) -> None:
    client, url = accuracy_site
    # Match a deployment's administrators disabling the feature at startup.
    for route in client.app.routes:
        if getattr(route, "path", None) == "/api/v1/accuracy/runs":
            for dependency in route.dependant.dependencies:
                if dependency.call.__name__ == "require_accuracy_enabled":

                    def disabled() -> None:
                        from fastapi import HTTPException

                        raise HTTPException(403, "accuracy evaluation is disabled")

                    client.app.dependency_overrides[dependency.call] = disabled
    open_site(browser, url)
    browser.click('[data-view="accuracy"]')
    browser.wait("!document.querySelector('#accuracy-unavailable').hidden")
    assert browser.evaluate(
        "document.querySelector('#accuracy-unavailable').textContent.includes('管理员关闭')"
    )
    assert browser.evaluate("document.querySelector('#open-accuracy-config').disabled")


def test_accuracy_browser_compares_matching_runs_and_refuses_different_settings(
    browser: Browser, accuracy_site: tuple[TestClient, str], tmp_path: Path
) -> None:
    client, url = accuracy_site
    client.headers["X-API-Key"] = "browser-test-key"
    make_cached_runner(client.app.state.accuracy_manager.adapter.evalscope_command)
    open_site(browser, url)
    browser.click(".accuracy-service")
    browser.fill('[name="gsm8k_limit"]', "2")
    browser.click("#start-accuracy")
    browser.wait("document.querySelectorAll('[data-accuracy-compare]').length===1")
    browser.click(".rerun-accuracy")
    assert not browser.evaluate("document.querySelector('#accuracy-cache-option').hidden")
    browser.click('[name="use_cache"]')
    browser.click("#start-accuracy")
    browser.wait("document.querySelectorAll('[data-accuracy-compare]').length===2")
    browser.click("[data-accuracy-compare]")
    browser.click(".accuracy-run-card:nth-child(2) [data-accuracy-compare]")
    browser.click("#compare-accuracy")
    browser.wait("document.querySelector('#accuracy-compare-content .accuracy-metric-table')")
    assert (
        browser.evaluate(
            "document.querySelectorAll('#accuracy-compare-content .accuracy-score').length"
        )
        == 2
    )
    browser.screenshot(tmp_path / "accuracy-comparison.png")
    browser.click("[data-accuracy-compare-close]")
    # Leave one original selected, create an incompatible temperature run through the real UI.
    browser.click("[data-accuracy-compare]")
    browser.click(".rerun-accuracy")
    browser.fill('[name="temperature"]', "0.5")
    browser.click("#start-accuracy")
    browser.wait("document.querySelectorAll('[data-accuracy-compare]').length===3")
    browser.click("[data-accuracy-compare]")
    browser.click("#compare-accuracy")
    browser.wait(
        "document.querySelector('#accuracy-compare-content').textContent.includes('不能直接比较')"
    )
    assert browser.evaluate(
        "document.querySelectorAll('#accuracy-compare-content table').length===0"
    )
    runs = client.app.state.accuracy_store.list()[0]
    assert any(run.cached_samples == 2 for run in runs)
    for run in runs:
        wait_terminal(client, run.id)
