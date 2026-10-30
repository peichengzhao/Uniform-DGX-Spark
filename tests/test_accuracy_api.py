from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from uniform_dgxspark.accuracy_store import AccuracyRunNotFound, AccuracyRunStore
from uniform_dgxspark.config import Settings
from uniform_dgxspark.database import Database
from uniform_dgxspark.main import create_app
from uniform_dgxspark.schemas import (
    AccuracyDatasetRequest,
    AccuracyRunCreate,
    DeploymentCreate,
    DeploymentParameters,
)
from uniform_dgxspark.services.evalscope_accuracy import (
    EXPECTED_EVALSCOPE_COMMIT,
    EXPECTED_EVALSCOPE_VERSION,
    EvalScopeAccuracyAdapter,
    EvalScopeIdentity,
)
from uniform_dgxspark.state import DeploymentStatus
from uniform_dgxspark.store import DeploymentStore


def make_fake_evalscope(path: Path, *, delay_seconds: float = 0) -> None:
    path.write_text(
        f"""#!/usr/bin/env python3
import json
import sys
import time
from pathlib import Path

args = sys.argv[1:]
def value(name):
    return args[args.index(name) + 1]

work = Path(value('--work-dir'))
model = value('--model')
dataset = value('--datasets')
work.mkdir(parents=True, exist_ok=True)
print('Authorization: Bearer bearer-secret', flush=True)
print('api_key=api-secret', flush=True)
print('proxy=http://proxy-user:proxy-pass@example.test', flush=True)
(work / 'progress.json').write_text(json.dumps({{
    'status': 'running', 'pipeline': 'eval', 'total_count': 2,
    'processed_count': 1, 'percent': 50.0
}}), encoding='utf-8')
time.sleep({delay_seconds!r})
report = {{
    'schema_version': 2,
    'name': model + '@' + dataset,
    'dataset_name': dataset,
    'dataset_pretty_name': 'GSM8K',
    'model_name': model,
    'metrics': [{{
        'identity': {{'name': 'accuracy', 'aggregation': 'mean', 'dimensions': {{}}}},
        'num': 2,
        'score': 1.0,
        'categories': [{{
            'name': ['default'], 'num': 2, 'score': 1.0,
            'subsets': [{{'name': 'main', 'score': 1.0, 'num': 2}}]
        }}],
        'semantics': {{
            'semantic_id': 'quality.accuracy.ratio',
            'metric_name': 'Accuracy',
            'display_name': None,
            'kind': 'quality',
            'direction': 'higher_is_better'
        }}
    }}],
    'primary_metric_identity': {{
        'name': 'accuracy', 'aggregation': 'mean', 'dimensions': {{}}
    }},
    'execution_summary': {{
        'requested': 2, 'succeeded': 2, 'errored': 0, 'incomplete': False,
        'subsets': {{
            'main': {{'requested': 2, 'succeeded': 2, 'errored': 0}}
        }}
    }}
}}
target = work / 'reports' / model / (dataset + '.json')
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(report), encoding='utf-8')
(work / 'progress.json').write_text(json.dumps({{
    'status': 'completed', 'pipeline': 'eval', 'total_count': 2,
    'processed_count': 2, 'percent': 100.0
}}), encoding='utf-8')
""",
        encoding="utf-8",
    )
    path.chmod(0o700)


def settings_for(tmp_path: Path, executable: Path, **overrides: object) -> Settings:
    values = {
        "data_dir": tmp_path,
        "worker_enabled": False,
        "accuracy_evalscope_command": executable,
        "accuracy_evalscope_source": tmp_path / "evalscope-source",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def mark_running(deployments: DeploymentStore, deployment_id: str) -> None:
    for status in (
        DeploymentStatus.PREFLIGHT,
        DeploymentStatus.DOWNLOADING_MODEL,
        DeploymentStatus.PULLING_IMAGE,
        DeploymentStatus.STARTING_CONTAINER,
        DeploymentStatus.LOADING_MODEL,
        DeploymentStatus.HEALTH_CHECKING,
        DeploymentStatus.RUNNING,
    ):
        deployments.transition(deployment_id, status, message="test")


def create_target(client: TestClient, *, running: bool = True) -> str:
    deployment = client.app.state.store.create(
        DeploymentCreate(
            model_id="org/model",
            display_name="Accuracy API model",
            parameters=DeploymentParameters(port=18000, served_model_name="served-model"),
        )
    )
    if running:
        mark_running(client.app.state.store, deployment.id)
    return deployment.id


def request_payload(target_id: str, *, subsets: list[str] | None = None) -> dict[str, object]:
    return {
        "target_type": "deployment",
        "target_id": target_id,
        "datasets": [
            {
                "name": "gsm8k",
                "subset_list": subsets or ["main"],
                "limit": 2,
                "few_shot_num": 0,
            }
        ],
    }


def accept_identity(client: TestClient) -> None:
    client.app.state.accuracy_manager.adapter.verify_identity = lambda: EvalScopeIdentity(
        version=EXPECTED_EVALSCOPE_VERSION,
        commit=EXPECTED_EVALSCOPE_COMMIT,
    )


def wait_for_status(
    client: TestClient,
    run_id: str,
    statuses: set[str],
    *,
    timeout: float = 5,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/accuracy/runs/{run_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in statuses:
            return payload
        time.sleep(0.02)
    raise AssertionError(f"accuracy run {run_id} did not reach {sorted(statuses)}")


def test_accuracy_api_completes_and_exposes_only_safe_artifacts(tmp_path: Path) -> None:
    executable = tmp_path / "fake-evalscope"
    make_fake_evalscope(executable)
    app = create_app(settings_for(tmp_path, executable))

    with TestClient(app) as client:
        assert isinstance(client.app.state.accuracy_store, AccuracyRunStore)
        target_id = create_target(client)
        accept_identity(client)

        created = client.post("/api/v1/accuracy/runs", json=request_payload(target_id))
        assert created.status_code == 201
        run_id = created.json()["id"]
        completed = wait_for_status(client, run_id, {"completed"})

        assert completed["summary"]["dataset_count"] == 1  # type: ignore[index]
        assert completed["metrics"][0]["score"] == 1  # type: ignore[index]
        listed = client.get("/api/v1/accuracy/runs").json()
        assert listed["total"] == 1
        assert listed["items"][0]["id"] == run_id

        run_directory = Path(str(completed["run_directory"]))
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "evalscope.log").write_text("outside-secret", encoding="utf-8")
        (run_directory / "evalscope" / "escape").symlink_to(outside, target_is_directory=True)

        log_response = client.get(f"/api/v1/accuracy/runs/{run_id}/logs")
        assert log_response.status_code == 200
        logs = log_response.json()["logs"]
        assert "Authorization: <redacted>" in logs
        assert "api_key=<redacted>" in logs
        assert "http://<redacted>@example.test" in logs
        assert "bearer-secret" not in logs
        assert "api-secret" not in logs
        assert "proxy-pass" not in logs
        assert "outside-secret" not in logs

        artifacts = client.get(f"/api/v1/accuracy/runs/{run_id}/artifacts").json()
        paths = {item["path"] for item in artifacts["items"]}
        assert "uniform-accuracy-config.json" in paths
        assert "evalscope/00-gsm8k/uniform-command.json" in paths
        assert "evalscope/00-gsm8k/progress.json" in paths
        assert "evalscope/00-gsm8k/reports/served-model/gsm8k.json" in paths
        assert all(path.endswith(".json") for path in paths)

        downloaded = client.get(
            f"/api/v1/accuracy/runs/{run_id}/artifacts/uniform-accuracy-config.json"
        )
        assert downloaded.status_code == 200
        assert downloaded.headers["content-type"].startswith("application/json")
        assert downloaded.headers["x-content-type-options"] == "nosniff"
        assert downloaded.json()["model_name"] == "served-model"
        assert (
            client.get(
                f"/api/v1/accuracy/runs/{run_id}/artifacts/evalscope/00-gsm8k/evalscope.log"
            ).status_code
            == 404
        )
        with pytest.raises(FileNotFoundError):
            client.app.state.accuracy_manager.artifact_path(run_id, "../../outside.json")


def test_accuracy_api_validates_targets_datasets_feature_flag_and_api_key(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "fake-evalscope"
    make_fake_evalscope(executable)
    app = create_app(settings_for(tmp_path, executable))
    with TestClient(app) as client:
        accept_identity(client)
        missing = client.post("/api/v1/accuracy/runs", json=request_payload("missing-deployment"))
        assert missing.status_code == 404

        stopped_target = create_target(client, running=False)
        stopped = client.post("/api/v1/accuracy/runs", json=request_payload(stopped_target))
        assert stopped.status_code == 409

        running_target = create_target(client)
        invalid = client.post(
            "/api/v1/accuracy/runs",
            json=request_payload(running_target, subsets=["not-a-gsm8k-subset"]),
        )
        assert invalid.status_code == 422

        original_create = client.app.state.accuracy_manager.create
        client.app.state.accuracy_manager.create = Mock(
            side_effect=OSError("simulated storage failure")
        )
        unavailable = client.post("/api/v1/accuracy/runs", json=request_payload(running_target))
        client.app.state.accuracy_manager.create = original_create
        assert unavailable.status_code == 503

        assert client.get("/api/v1/accuracy/runs/missing-run").status_code == 404
        assert client.post("/api/v1/accuracy/runs/missing-run/cancel").status_code == 404
        assert client.delete("/api/v1/accuracy/runs/missing-run").status_code == 404

    disabled_app = create_app(
        settings_for(tmp_path / "disabled", executable, accuracy_enabled=False)
    )
    with TestClient(disabled_app) as client:
        assert client.get("/api/v1/accuracy/runs").status_code == 403

    protected_app = create_app(
        settings_for(tmp_path / "protected", executable, api_token="test-api-key")
    )
    with TestClient(protected_app) as client:
        assert client.get("/api/v1/accuracy/runs").status_code == 401
        assert (
            client.get("/api/v1/accuracy/runs", headers={"X-API-Key": "test-api-key"}).status_code
            == 200
        )


def test_accuracy_api_cancels_and_deletes_run_and_directory(tmp_path: Path) -> None:
    executable = tmp_path / "slow-evalscope"
    make_fake_evalscope(executable, delay_seconds=30)
    app = create_app(settings_for(tmp_path, executable))

    with TestClient(app) as client:
        target_id = create_target(client)
        accept_identity(client)
        created = client.post("/api/v1/accuracy/runs", json=request_payload(target_id))
        assert created.status_code == 201
        run_id = created.json()["id"]
        running = wait_for_status(client, run_id, {"running"})
        run_directory = Path(str(running["run_directory"]))

        assert client.delete(f"/api/v1/accuracy/runs/{run_id}").status_code == 409
        cancelled = client.post(f"/api/v1/accuracy/runs/{run_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and client.app.state.accuracy_manager.is_active(run_id):
            time.sleep(0.02)
        assert not client.app.state.accuracy_manager.is_active(run_id)
        assert client.delete(f"/api/v1/accuracy/runs/{run_id}").status_code == 204
        assert not run_directory.exists()
        with pytest.raises(AccuracyRunNotFound):
            client.app.state.accuracy_store.get(run_id)


def test_accuracy_lifecycle_recovers_interrupted_runs_and_stops_manager(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "fake-evalscope"
    make_fake_evalscope(executable)
    database = Database(tmp_path / "uniform.db")
    database.initialize()
    deployments = DeploymentStore(database)
    accuracy_store = AccuracyRunStore(database)
    deployment = deployments.create(DeploymentCreate(model_id="org/model"))
    request = AccuracyRunCreate(
        target_type="deployment",
        target_id=deployment.id,
        datasets=[AccuracyDatasetRequest(name="gsm8k", subset_list=["main"], limit=2)],
    )
    adapter = EvalScopeAccuracyAdapter(executable, tmp_path / "evalscope-source")
    resolved = adapter.resolve_config(request, {"status": "running", "port": 18000})
    run_directory = tmp_path / "accuracy-runs" / "interrupted-run"
    run_directory.mkdir(parents=True)
    queued = accuracy_store.create(
        request,
        target_label="Interrupted model",
        model_name="org/model",
        service_url="http://127.0.0.1:18000",
        resolved_config=resolved,
        evalscope_version=EXPECTED_EVALSCOPE_VERSION,
        evalscope_commit=EXPECTED_EVALSCOPE_COMMIT,
        run_directory=run_directory,
    )

    app = create_app(settings_for(tmp_path, executable))
    stop_all = Mock()
    with TestClient(app) as client:
        client.app.state.accuracy_manager.stop_all = stop_all
        health = client.get("/api/v1/health").json()
        recovered = client.get(f"/api/v1/accuracy/runs/{queued.id}").json()
        assert health["recovered_tasks"] == 1
        assert recovered["status"] == "interrupted"
        assert recovered["error"]["code"] == "control_service_restarted"

    stop_all.assert_called_once_with()
