from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from uniform_dgxspark.accuracy_store import AccuracyRunStore
from uniform_dgxspark.ai_store import AISessionStore
from uniform_dgxspark.database import Database
from uniform_dgxspark.schemas import (
    AccuracyDatasetRequest,
    AccuracyRunCreate,
    DeploymentCreate,
    DeploymentParameters,
)
from uniform_dgxspark.services.accuracy import AccuracyBusy, AccuracyManager
from uniform_dgxspark.services.evalscope_accuracy import (
    EXPECTED_EVALSCOPE_COMMIT,
    EXPECTED_EVALSCOPE_VERSION,
    EvalScopeAccuracyAdapter,
    EvalScopeAccuracyError,
    EvalScopeIdentity,
    EvalScopeIdentityError,
    EvalScopeReportError,
)
from uniform_dgxspark.state import DeploymentStatus
from uniform_dgxspark.store import DeploymentStore


def make_request(
    target_id: str,
    *,
    limit: int = 2,
    subsets: list[str] | None = None,
) -> AccuracyRunCreate:
    return AccuracyRunCreate(
        target_type="deployment",
        target_id=target_id,
        datasets=[
            AccuracyDatasetRequest(
                name="gsm8k",
                subset_list=subsets or ["main"],
                limit=limit,
                few_shot_num=0,
            )
        ],
    )


def report_payload(
    model_name: str = "served-model",
    *,
    schema_version: int = 2,
    requested: int = 2,
    succeeded: int = 2,
    errored: int = 0,
    incomplete: bool = False,
    score: float = 1.0,
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "name": f"{model_name}@gsm8k",
        "dataset_name": "gsm8k",
        "dataset_pretty_name": "GSM8K",
        "model_name": model_name,
        "metrics": [
            {
                "identity": {"name": "accuracy", "aggregation": "mean", "dimensions": {}},
                "num": succeeded,
                "score": score,
                "categories": [
                    {
                        "name": ["default"],
                        "num": succeeded,
                        "score": score,
                        "subsets": [{"name": "main", "score": score, "num": succeeded}],
                    }
                ],
                "semantics": {
                    "semantic_id": "quality.accuracy.ratio",
                    "metric_name": "Accuracy",
                    "display_name": None,
                    "kind": "quality",
                    "direction": "higher_is_better",
                },
            }
        ],
        "primary_metric_identity": {
            "name": "accuracy",
            "aggregation": "mean",
            "dimensions": {},
        },
        "execution_summary": {
            "requested": requested,
            "succeeded": succeeded,
            "errored": errored,
            "incomplete": incomplete,
            "subsets": {
                "main": {
                    "requested": requested,
                    "succeeded": succeeded,
                    "errored": errored,
                }
            },
        },
    }


def write_report(work_directory: Path, payload: dict[str, Any]) -> None:
    path = work_directory / "reports" / str(payload["model_name"]) / "gsm8k.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def make_fake_evalscope(
    path: Path,
    payload: dict[str, Any] | None,
    *,
    exit_code: int = 0,
    delay_seconds: float = 0,
) -> None:
    serialized = json.dumps(payload) if payload is not None else "null"
    path.write_text(
        f"""#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
def value(name):
    return args[args.index(name) + 1]

work = Path(value('--work-dir'))
work.mkdir(parents=True, exist_ok=True)
(work / 'child-environment.json').write_text(json.dumps(dict(os.environ)), encoding='utf-8')
(work / 'progress.json').write_text(json.dumps({{
    'status': 'running', 'pipeline': 'eval', 'total_count': 2,
    'processed_count': 1, 'percent': 50.0
}}), encoding='utf-8')
time.sleep({delay_seconds!r})
payload = json.loads({serialized!r})
if payload is not None:
    payload['model_name'] = value('--model')
    target = work / 'reports' / value('--model') / (value('--datasets') + '.json')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding='utf-8')
    processed = payload['execution_summary']['succeeded']
    processed += payload['execution_summary']['errored']
    (work / 'progress.json').write_text(json.dumps({{
        'status': 'completed', 'pipeline': 'eval',
        'total_count': payload['execution_summary']['requested'],
        'processed_count': processed,
        'percent': 100.0
    }}), encoding='utf-8')
raise SystemExit({exit_code})
""",
        encoding="utf-8",
    )
    path.chmod(0o700)


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


def make_manager(
    tmp_path: Path,
    fake_evalscope: Path,
) -> tuple[AccuracyManager, AccuracyRunStore, str, EvalScopeAccuracyAdapter]:
    database = Database(tmp_path / "uniform.db")
    database.initialize()
    deployments = DeploymentStore(database)
    sessions = AISessionStore(database)
    store = AccuracyRunStore(database)
    deployment = deployments.create(
        DeploymentCreate(
            model_id="org/model",
            display_name="Accuracy model",
            parameters=DeploymentParameters(port=8000, served_model_name="served-model"),
        )
    )
    mark_running(deployments, deployment.id)
    adapter = EvalScopeAccuracyAdapter(fake_evalscope, tmp_path / "evalscope-source")
    adapter.verify_identity = lambda: EvalScopeIdentity(  # type: ignore[method-assign]
        version=EXPECTED_EVALSCOPE_VERSION,
        commit=EXPECTED_EVALSCOPE_COMMIT,
    )
    manager = AccuracyManager(
        store,
        deployments,
        sessions,
        tmp_path / "accuracy-runs",
        adapter,
    )
    return manager, store, deployment.id, adapter


def wait_terminal(store: AccuracyRunStore, run_id: str, timeout: float = 5) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = store.get(run_id)
        if run.status in {"completed", "partial", "failed", "cancelled"}:
            return run
        time.sleep(0.02)
    raise AssertionError(f"accuracy run {run_id} did not finish")


def test_adapter_builds_controlled_command_and_normalizes_schema_v2(tmp_path: Path) -> None:
    executable = tmp_path / "evalscope"
    executable.touch()
    adapter = EvalScopeAccuracyAdapter(executable, tmp_path)
    request = make_request("deployment-1")
    config = adapter.resolve_config(request, {"status": "running", "port": 8000})
    work = tmp_path / "work"
    write_report(work, report_payload())

    command = adapter.build_command(
        model_name="served-model",
        service_url="http://127.0.0.1:8000",
        dataset=config.datasets[0],
        config=config,
        run_directory=tmp_path / "run",
        work_directory=work,
    )
    normalized = adapter.normalize_report(work, config.datasets[0], "served-model")

    assert command[0] == str(executable.resolve())
    assert "--api-key" in command and command[command.index("--api-key") + 1] == "EMPTY"
    assert "--no-timestamp" in command
    assert normalized.execution_succeeded == 2
    assert normalized.metrics[0].metric_key == "accuracy:mean"
    assert normalized.metrics[0].score == 1
    assert normalized.metrics[0].is_primary

    with pytest.raises(EvalScopeAccuracyError, match="model service"):
        adapter.build_command(
            model_name="served-model",
            service_url="http://127.0.0.1:8000@evil.example",
            dataset=config.datasets[0],
            config=config,
            run_directory=tmp_path / "run",
            work_directory=work,
        )


def test_adapter_verifies_exact_evalscope_version_and_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "evalscope"
    executable.touch()
    source = tmp_path / "source"
    source.mkdir()
    adapter = EvalScopeAccuracyAdapter(executable, source)
    outputs = iter(
        [
            f"evalscope {EXPECTED_EVALSCOPE_VERSION}\n",
            f"{EXPECTED_EVALSCOPE_COMMIT}\n",
        ]
    )

    def fake_run(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        return subprocess.CompletedProcess([], 0, stdout=next(outputs))

    monkeypatch.setattr(subprocess, "run", fake_run)
    identity = adapter.verify_identity()

    assert identity.version == EXPECTED_EVALSCOPE_VERSION
    assert identity.commit == EXPECTED_EVALSCOPE_COMMIT


def test_adapter_rejects_unknown_subset_schema_and_escaping_artifact(tmp_path: Path) -> None:
    executable = tmp_path / "evalscope"
    executable.touch()
    adapter = EvalScopeAccuracyAdapter(executable, tmp_path)
    with pytest.raises(EvalScopeAccuracyError, match="unsupported gsm8k subsets"):
        adapter.resolve_config(make_request("deployment-1", subsets=["other"]), {})

    config = adapter.resolve_config(make_request("deployment-1"), {})
    work = tmp_path / "work"
    write_report(work, report_payload(schema_version=3))
    with pytest.raises(EvalScopeReportError, match="schema_version=3"):
        adapter.normalize_report(work, config.datasets[0], "served-model")

    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(report_payload()), encoding="utf-8")
    report = work / "reports" / "served-model" / "gsm8k.json"
    report.unlink()
    report.symlink_to(outside)
    with pytest.raises(EvalScopeReportError, match="escapes"):
        adapter.normalize_report(work, config.datasets[0], "served-model")


def test_manager_completes_and_does_not_inherit_unrelated_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "fake-evalscope"
    make_fake_evalscope(fake, report_payload())
    manager, store, deployment_id, _adapter = make_manager(tmp_path, fake)
    monkeypatch.setenv("UNIFORM_STAGE38_SECRET", "must-not-reach-child")

    created = manager.create(make_request(deployment_id))
    completed = wait_terminal(store, created.id)

    assert completed.status == "completed"
    assert completed.progress.percent == 100
    assert completed.summary is not None and completed.summary.dataset_count == 1
    assert completed.metrics[0].score == 1
    run_directory = Path(completed.run_directory)
    child_environment = json.loads(
        next(run_directory.glob("evalscope/*/child-environment.json")).read_text()
    )
    assert "UNIFORM_STAGE38_SECRET" not in child_environment
    command = json.loads(next(run_directory.glob("evalscope/*/uniform-command.json")).read_text())
    assert command[0] == str(fake.resolve())
    assert not manager.is_active(created.id)


def test_manager_persists_partial_and_rejects_unknown_report_schema(tmp_path: Path) -> None:
    partial_fake = tmp_path / "partial-evalscope"
    make_fake_evalscope(
        partial_fake,
        report_payload(
            requested=2,
            succeeded=1,
            errored=1,
            incomplete=True,
            score=0.5,
        ),
    )
    manager, store, deployment_id, _adapter = make_manager(tmp_path, partial_fake)

    partial = wait_terminal(store, manager.create(make_request(deployment_id)).id)

    assert partial.status == "partial"
    assert partial.summary is not None and partial.summary.incomplete
    assert partial.metrics[0].execution_errored == 1

    invalid_root = tmp_path / "invalid"
    invalid_root.mkdir()
    invalid_fake = invalid_root / "evalscope"
    make_fake_evalscope(invalid_fake, report_payload(schema_version=3))
    invalid_manager, invalid_store, invalid_deployment, _ = make_manager(invalid_root, invalid_fake)
    failed = wait_terminal(
        invalid_store,
        invalid_manager.create(make_request(invalid_deployment)).id,
    )
    assert failed.status == "failed"
    assert failed.error is not None
    assert failed.error.code == "evalscope_report_invalid"
    assert not failed.error.retryable


def test_manager_cancels_the_evalscope_process_group(tmp_path: Path) -> None:
    fake = tmp_path / "slow-evalscope"
    make_fake_evalscope(fake, report_payload(), delay_seconds=30)
    manager, store, deployment_id, _adapter = make_manager(tmp_path, fake)
    created = manager.create(make_request(deployment_id))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and store.get(created.id).status != "running":
        time.sleep(0.01)

    with pytest.raises(AccuracyBusy, match="already running"):
        manager.create(make_request(deployment_id))

    cancelled = manager.cancel(created.id)
    terminal = wait_terminal(store, created.id)

    assert cancelled.status == "cancelled"
    assert terminal.status == "cancelled"
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and manager.is_active(created.id):
        time.sleep(0.01)
    assert not manager.is_active(created.id)


def test_manager_rejects_unverified_evalscope_identity(tmp_path: Path) -> None:
    fake = tmp_path / "fake-evalscope"
    make_fake_evalscope(fake, report_payload())
    manager, store, deployment_id, adapter = make_manager(tmp_path, fake)

    def reject_identity() -> EvalScopeIdentity:
        raise EvalScopeIdentityError("unexpected EvalScope commit")

    adapter.verify_identity = reject_identity  # type: ignore[method-assign]
    failed = wait_terminal(store, manager.create(make_request(deployment_id)).id)

    assert failed.status == "failed"
    assert failed.error is not None
    assert failed.error.code == "evalscope_identity_invalid"
    assert not failed.error.retryable


def test_manager_records_retryable_process_failure(tmp_path: Path) -> None:
    fake = tmp_path / "failed-evalscope"
    make_fake_evalscope(fake, None, exit_code=7)
    manager, store, deployment_id, _adapter = make_manager(tmp_path, fake)

    failed = wait_terminal(store, manager.create(make_request(deployment_id)).id)

    assert failed.status == "failed"
    assert failed.error is not None
    assert failed.error.code == "evalscope_failed"
    assert failed.error.retryable
