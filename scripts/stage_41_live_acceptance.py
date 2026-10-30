"""Opt-in live acceptance: real registered model, isolated control plane/database.

Run explicitly with PYTHONPATH=tests .venv/bin/python -m pytest -s -q <this file>.
This is deliberately outside tests/ so normal regression never generates model traffic.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from test_accuracy_browser import Browser, open_site
from test_accuracy_browser import browser as browser

from uniform_dgxspark.config import Settings
from uniform_dgxspark.main import create_app
from uniform_dgxspark.schemas import DeploymentCreate, DeploymentParameters
from uniform_dgxspark.state import DeploymentStatus


def registered_target(source: Path) -> tuple[sqlite3.Row, str, int]:
    with sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT model_id,revision,parameters_json FROM deployments "
            "WHERE status='running' AND archived_at IS NULL"
        ).fetchall()
    with httpx.Client(trust_env=False, timeout=5) as client:
        for row in rows:
            parameters = json.loads(row["parameters_json"])
            port = parameters["port"]
            model = parameters.get("served_model_name") or row["model_id"]
            try:
                models = client.get(f"http://127.0.0.1:{port}/v1/models")
                if models.status_code == 200 and model in {
                    item["id"] for item in models.json().get("data", [])
                }:
                    return row, model, port
            except (httpx.HTTPError, ValueError):
                continue
    pytest.fail("No registered running deployment exposes its model on a loopback endpoint")


@pytest.fixture
def live_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, str]]:
    project = Path(__file__).resolve().parents[1]
    source = Path(os.getenv("UNIFORM_STAGE41_SOURCE_DB", str(project / "data" / "uniform.db")))
    row, model, port = registered_target(source)
    command = Path(os.environ["UNIFORM_STAGE41_EVALSCOPE_COMMAND"])
    settings = Settings(
        data_dir=tmp_path / "isolated-data",
        worker_enabled=False,
        api_token="browser-test-key",
        accuracy_evalscope_command=command,
        accuracy_evalscope_source=Path(os.environ["UNIFORM_STAGE41_EVALSCOPE_SOURCE"]),
    )
    # Only the temporary control plane is patched. No production Docker mutations occur.
    runtime = "uniform_dgxspark.services.docker_runtime.DockerRuntime."
    monkeypatch.setattr(runtime + "discover_ai_services", lambda _: [])
    monkeypatch.setattr(runtime + "list_images", lambda _: [])
    monkeypatch.setattr(runtime + "is_running", lambda *_: True)
    monkeypatch.setattr(runtime + "health", lambda *_, **__: True)
    with TestClient(create_app(settings)) as client:
        client.headers["X-API-Key"] = "browser-test-key"
        client.app.state.accuracy_manager.adapter.verify_identity()
        target = client.app.state.store.create(
            DeploymentCreate(
                model_id=row["model_id"],
                revision=row["revision"],
                display_name="阶段 41 真实验收",
                parameters=DeploymentParameters(port=port, served_model_name=model),
            )
        )
        for state in (
            DeploymentStatus.PREFLIGHT,
            DeploymentStatus.DOWNLOADING_MODEL,
            DeploymentStatus.PULLING_IMAGE,
            DeploymentStatus.STARTING_CONTAINER,
            DeploymentStatus.LOADING_MODEL,
            DeploymentStatus.HEALTH_CHECKING,
            DeploymentStatus.RUNNING,
        ):
            client.app.state.store.transition(target.id, state, message="verified live target copy")
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
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
            print(f"LIVE_MODEL {model} :{port}; isolated evidence {tmp_path}", flush=True)
            yield client, f"http://127.0.0.1:{listener.getsockname()[1]}"
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            listener.close()
            assert not thread.is_alive()


def wait_live(client: TestClient, run_id: str, expected: str = "completed") -> dict:
    deadline = time.monotonic() + 2700
    next_notice = 0.0
    while time.monotonic() < deadline:
        run = client.get(f"/api/v1/accuracy/runs/{run_id}").json()
        if time.monotonic() >= next_notice:
            print("LIVE_PROGRESS", run_id, run["status"], run["progress"], flush=True)
            next_notice = time.monotonic() + 30
        if run["status"] not in {"queued", "preparing", "running", "parsing"}:
            assert run["status"] == expected, run.get("error")
            while client.app.state.accuracy_manager.is_active(run_id):
                assert time.monotonic() < deadline
                time.sleep(0.05)
            return client.get(f"/api/v1/accuracy/runs/{run_id}").json()
        time.sleep(1)
    raise AssertionError("live evaluation did not reach a terminal state within 45 minutes")


@pytest.fixture
def live_evidence_dir() -> Path:
    configured = os.getenv("UNIFORM_STAGE41_EVIDENCE_DIR")
    directory = (
        Path(configured).resolve()
        if configured
        else Path(tempfile.mkdtemp(prefix="uniform-stage41-evidence-"))
    )
    directory.mkdir(parents=True, exist_ok=True)
    print(f"LIVE_EVIDENCE {directory}; retained outside Pytest automatic cleanup", flush=True)
    return directory


def test_real_model_quality_cache_recovery_comparison_and_download(
    browser: Browser, live_site: tuple[TestClient, str], live_evidence_dir: Path
) -> None:
    client, url = live_site
    open_site(browser, url)
    evidence = []

    def submit(limit: int, *, rerun: str | None = None, cache: bool = False) -> dict:
        if rerun:
            browser.wait(f'document.querySelector(\'.rerun-accuracy[data-id="{rerun}"]\')')
        browser.click(f'.rerun-accuracy[data-id="{rerun}"]' if rerun else ".accuracy-service")
        browser.fill("#accuracy-preset", "quick-objective-v1")
        browser.fill('[name="gsm8k_limit"]', str(limit))
        if cache:
            browser.click('[name="use_cache"]')
        browser.click("#start-accuracy")
        browser.wait("!document.querySelector('#accuracy-config-dialog').open")
        run = client.app.state.accuracy_store.list()[0][0]
        result = wait_live(client, run.id)
        evidence.append(result)
        (live_evidence_dir / "stage-41-live-results.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result

    small = submit(2)
    quick = submit(20, rerun=small["id"])
    assert quick["summary"]["execution_succeeded"] == 20
    replay = submit(20, rerun=quick["id"], cache=True)
    assert replay["cached_samples"] == 20
    browser.wait(f"document.querySelector('[data-accuracy-compare=\"{replay['id']}\"]')")
    for run in (quick, replay):
        browser.click(f'[data-accuracy-compare="{run["id"]}"]')
    browser.click("#compare-accuracy")
    browser.wait("document.querySelector('#accuracy-compare-content table')")
    browser.screenshot(live_evidence_dir / "live-accuracy-comparison.png")
    browser.click("[data-accuracy-compare-close]")

    # Exercise a real changed generation contract without spending another 20 requests.
    payload = small["request"].copy()
    payload["generation_config"] = {**payload["generation_config"], "top_p": 0.99}
    changed = client.post("/api/v1/accuracy/runs", json=payload)
    assert changed.status_code == 201, changed.text
    altered = wait_live(client, changed.json()["id"])
    evidence.append(altered)
    comparison = client.post(
        "/api/v1/accuracy/compare", json={"run_ids": [small["id"], altered["id"]]}
    )
    assert comparison.status_code == 409

    # Exercise cancellation during preparation through the real control plane.
    # Running-process cancellation is verified by the separate live test below.
    running = client.post("/api/v1/accuracy/runs", json=quick["request"]).json()
    browser.wait(f"document.querySelector('.cancel-accuracy[data-id=\"{running['id']}\"]')")
    browser.click(f'.cancel-accuracy[data-id="{running["id"]}"]')
    evidence.append(wait_live(client, running["id"], expected="cancelled"))

    # Simulate control-plane interruption only in this isolated instance, with cached
    # predictions. The production service is not restarted and the source is immutable.
    interrupted = client.post(
        "/api/v1/accuracy/runs",
        json={**quick["request"], "use_cache": True, "cache_source_run_id": quick["id"]},
    ).json()
    manager = client.app.state.accuracy_manager
    deadline = time.monotonic() + 15
    process = None
    while process is None and time.monotonic() < deadline:
        with manager._lock:
            active = manager._active.get(interrupted["id"])
            process = active.process if active else None
            if process:
                active.cancel_event.set()
                client.app.state.accuracy_store.recover_incomplete()
        time.sleep(0.05)
    assert process is not None
    manager._terminate_process(process)
    evidence.append(wait_live(client, interrupted["id"], expected="interrupted"))
    resumed = client.post(f"/api/v1/accuracy/runs/{interrupted['id']}/resume")
    assert resumed.status_code == 201, resumed.text
    recovered = wait_live(client, resumed.json()["id"])
    assert recovered["cached_samples"] == 20
    evidence.append(recovered)

    payload = {
        **small["request"],
        "preset": "custom-objective-v1",
        "datasets": [
            {
                "name": "ceval",
                "subset_list": ["computer_network", "operating_system"],
                "limit": 2,
                "few_shot_num": 0,
            },
            {"name": "arc", "subset_list": ["ARC-Easy"], "limit": 2, "few_shot_num": 0},
        ],
    }
    created = client.post("/api/v1/accuracy/runs", json=payload)
    assert created.status_code == 201, created.text
    chinese = wait_live(client, created.json()["id"])
    assert chinese["summary"]["execution_succeeded"] == 6
    assert (
        sum(
            metric["sample_count"]
            for metric in chinese["metrics"]
            if metric["dataset_name"] == "ceval" and metric["is_primary"]
        )
        == 4
    )
    evidence.append(chinese)

    for run in (small, quick, replay, recovered, chinese):
        artifacts = client.get(f"/api/v1/accuracy/runs/{run['id']}/artifacts").json()
        reports = [item["path"] for item in artifacts["items"] if "/reports/" in item["path"]]
        assert reports
        for path in reports:
            response = client.get(f"/api/v1/accuracy/runs/{run['id']}/artifacts/{path}")
            assert response.status_code == 200
            report = response.json()
            assert report["schema_version"] == 2 and report["model_name"] == run["model_name"]
            assert path.split("/")[-1] in response.headers["content-disposition"]
            report_dir = live_evidence_dir / "reports"
            report_dir.mkdir(exist_ok=True)
            (report_dir / f"{run['id']}-{path.split('/')[-1]}").write_bytes(response.content)
    browser.wait(f"document.querySelector('.accuracy-detail[data-id=\"{quick['id']}\"]')")
    browser.click(f'.accuracy-detail[data-id="{quick["id"]}"]')
    browser.wait("document.querySelector('#accuracy-detail-content .accuracy-score')")
    assert (
        browser.evaluate(
            "document.querySelector('#accuracy-detail-content .accuracy-score').textContent"
        )
        == f"{quick['metrics'][0]['score'] * 100:.2f}%"
    )
    browser.screenshot(live_evidence_dir / "live-accuracy-result.png")
    with client.app.state.database.connect() as connection:
        assert "browser-test-key" not in "\n".join(connection.iterdump())
    for run in evidence:
        assert "browser-test-key" not in manager.logs(run["id"]).logs
    (live_evidence_dir / "stage-41-live-results.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("LIVE_PASS GSM8K 2/20, C-Eval 2x2 + ARC 2, cache/recovery/compare/download", flush=True)


def test_real_model_running_cancellation(
    browser: Browser, live_site: tuple[TestClient, str], live_evidence_dir: Path
) -> None:
    """Do not mistake a preparing-state cancellation for subprocess termination."""
    client, url = live_site
    open_site(browser, url)
    browser.click(".accuracy-service")
    browser.fill("#accuracy-preset", "quick-objective-v1")
    browser.fill('[name="gsm8k_limit"]', "20")
    browser.click("#start-accuracy")
    browser.wait("!document.querySelector('#accuracy-config-dialog').open")
    run = client.app.state.accuracy_store.list()[0][0]
    manager = client.app.state.accuracy_manager
    deadline = time.monotonic() + 60
    process = None
    while time.monotonic() < deadline:
        with manager._lock:
            active = manager._active.get(run.id)
            candidate = active.process if active else None
        state = client.get(f"/api/v1/accuracy/runs/{run.id}").json()
        if candidate is not None and candidate.poll() is None and state["status"] == "running":
            process = candidate
            break
        assert state["status"] in {"queued", "preparing", "running"}, state.get("error")
        time.sleep(0.05)
    assert process is not None, "live EvalScope did not enter running state within 60 seconds"
    started_pid = process.pid
    browser.wait(f"document.querySelector('.cancel-accuracy[data-id=\"{run.id}\"]')")
    browser.click(f'.cancel-accuracy[data-id="{run.id}"]')
    cancelled = wait_live(client, run.id, expected="cancelled")
    assert cancelled["started_at"] is not None
    assert process.poll() is not None, "cancelled live EvalScope subprocess is still running"
    with httpx.Client(trust_env=False, timeout=5) as model_client:
        response = model_client.get(f"{cancelled['service_url'].rstrip('/')}/v1/models")
        assert response.status_code == 200
        assert cancelled["model_name"] in {item["id"] for item in response.json()["data"]}
    assert "browser-test-key" not in manager.logs(run.id).logs
    (live_evidence_dir / "stage-41-live-cancellation.json").write_text(
        json.dumps(
            {
                "run": cancelled,
                "started_pid": started_pid,
                "process_returncode": process.returncode,
                "model_still_accessible": True,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        "LIVE_PASS running EvalScope cancellation, process exit, model still accessible",
        flush=True,
    )
