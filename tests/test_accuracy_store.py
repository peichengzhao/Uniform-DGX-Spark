from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from uniform_dgxspark.accuracy_store import (
    AccuracyRunNotFound,
    AccuracyRunStore,
    AccuracyRunTransitionError,
)
from uniform_dgxspark.database import SCHEMA_VERSION, Database
from uniform_dgxspark.schemas import (
    AccuracyDatasetRequest,
    AccuracyMetricRead,
    AccuracyResolvedConfig,
    AccuracyResolvedDataset,
    AccuracyRunCreate,
    AccuracyRunError,
    AccuracyRunProgress,
    AccuracyRunSummary,
)
from uniform_dgxspark.store import DeploymentStore

EVALSCOPE_COMMIT = "ccb9019ff56fd7dcc8602f4d17b706b31228edca"
PROMPT_HASH = f"sha256:{'a' * 64}"


def make_request(*, target_id: str = "deployment-1") -> AccuracyRunCreate:
    return AccuracyRunCreate(
        target_type="deployment",
        target_id=target_id,
        datasets=[
            AccuracyDatasetRequest(
                name="gsm8k",
                subset_list=["main"],
                limit=2,
                few_shot_num=0,
            )
        ],
    )


def make_resolved_config() -> AccuracyResolvedConfig:
    return AccuracyResolvedConfig(
        datasets=[
            AccuracyResolvedDataset(
                name="gsm8k",
                dataset_id="AI-ModelScope/gsm8k",
                dataset_hub="modelscope",
                dataset_revision="master",
                content_hash=None,
                evaluation_version="v1.1",
                subset_list=["main"],
                limit=2,
                few_shot_num=0,
                prompt_hash=PROMPT_HASH,
            )
        ],
        generation_config={"temperature": 0, "max_tokens": 2048, "top_p": 1},
        eval_batch_size=1,
        timeout_seconds=300,
        seed=42,
        deployment_snapshot={"status": "running", "port": 8000},
    )


def make_metric(*, score: float = 1.0) -> AccuracyMetricRead:
    return AccuracyMetricRead(
        dataset_name="gsm8k",
        dataset_pretty_name="GSM8K",
        evaluation_version="v1.1",
        metric_key="accuracy:mean",
        metric_display_name="Accuracy",
        metric_kind="quality",
        higher_is_better=True,
        category_path=["default"],
        subset_name="main",
        sample_count=2,
        score=score,
        is_primary=True,
        execution_requested=2,
        execution_succeeded=2,
        execution_errored=0,
    )


def make_summary(*, incomplete: bool = False) -> AccuracyRunSummary:
    return AccuracyRunSummary(
        dataset_count=1,
        metric_count=1,
        primary_metric_count=1,
        execution_requested=2,
        execution_succeeded=1 if incomplete else 2,
        execution_errored=1 if incomplete else 0,
        incomplete=incomplete,
    )


def make_store(tmp_path: Path) -> tuple[Database, AccuracyRunStore]:
    database = Database(tmp_path / "uniform.db")
    database.initialize()
    return database, AccuracyRunStore(database)


def create_run(store: AccuracyRunStore, tmp_path: Path, *, name: str = "run") -> object:
    return store.create(
        make_request(target_id=name),
        target_label="Local model",
        model_name="served-model",
        service_url="http://127.0.0.1:8000/v1",
        resolved_config=make_resolved_config(),
        evalscope_version="0.0.0_dev",
        evalscope_commit=EVALSCOPE_COMMIT,
        run_directory=tmp_path / "accuracy-runs" / name,
    )


def test_accuracy_contracts_reject_unknown_and_invalid_values() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AccuracyRunCreate.model_validate(
            {
                "target_type": "deployment",
                "target_id": "deployment-1",
                "datasets": [{"name": "gsm8k", "subset_list": ["main"], "limit": 2}],
                "command": "evalscope eval",
            }
        )
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        AccuracyDatasetRequest(name="gsm8k", subset_list=["main"], limit=0)
    with pytest.raises(ValidationError, match="dataset names must be unique"):
        AccuracyRunCreate(
            target_type="deployment",
            target_id="deployment-1",
            datasets=[
                AccuracyDatasetRequest(name="gsm8k", subset_list=["main"], limit=2),
                AccuracyDatasetRequest(name="gsm8k", subset_list=["main"], limit=3),
            ],
        )
    with pytest.raises(ValidationError, match="processed_samples cannot exceed"):
        AccuracyRunProgress(processed_samples=2, total_samples=1)
    with pytest.raises(ValidationError, match="execution counts cannot exceed"):
        AccuracyMetricRead(
            **make_metric().model_dump(exclude={"execution_succeeded"}),
            execution_succeeded=3,
        )


def test_new_database_has_stage_37_tables_and_indexes(tmp_path: Path) -> None:
    database, _store = make_store(tmp_path)

    with database.connect() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        indexes = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        versions = [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]

    assert SCHEMA_VERSION == 26
    assert {"accuracy_runs", "accuracy_run_metrics"} <= tables
    assert {
        "idx_accuracy_runs_created",
        "idx_accuracy_runs_status",
        "idx_accuracy_runs_target",
        "idx_accuracy_metrics_run_dataset",
        "idx_accuracy_metrics_primary",
    } <= indexes
    assert versions[-1] == 26


def test_stage_35_database_upgrades_without_losing_existing_deployment(tmp_path: Path) -> None:
    path = tmp_path / "uniform.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO schema_migrations VALUES(20, '2026-10-21T00:00:00Z');
            CREATE TABLE deployments (
                id TEXT PRIMARY KEY, model_id TEXT NOT NULL, revision TEXT, display_name TEXT,
                parameters_json TEXT NOT NULL, status TEXT NOT NULL, progress REAL NOT NULL,
                error_code TEXT, error_message TEXT, attempt INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 1, archived_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO deployments(
                id, model_id, display_name, parameters_json, status, progress,
                attempt, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "existing-deployment",
                "org/existing-model",
                "Existing model",
                json.dumps(
                    {
                        "port": 8000,
                        "source": "huggingface",
                        "image": "engine:test",
                        "served_model_name": None,
                        "local_model_path": None,
                        "max_model_len": None,
                        "gpu_memory_utilization": 0.9,
                        "startup_timeout_seconds": None,
                        "trust_remote_code": False,
                        "remote_code_acknowledged": False,
                        "extra_args": [],
                    }
                ),
                "stopped",
                1,
                0,
                1,
                "2026-10-21T00:00:00Z",
                "2026-10-21T00:00:00Z",
            ),
        )
        connection.commit()

    database = Database(path)
    database.initialize()

    deployment = DeploymentStore(database).get("existing-deployment")
    with database.connect() as connection:
        versions = [
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        accuracy_tables = connection.execute(
            """
            SELECT COUNT(*) FROM sqlite_master
            WHERE type = 'table' AND name IN ('accuracy_runs', 'accuracy_run_metrics')
            """
        ).fetchone()[0]

    assert deployment.model_id == "org/existing-model"
    assert deployment.status == "stopped"
    assert versions == [20, 26]
    assert accuracy_tables == 2


def test_create_get_and_list_round_trip(tmp_path: Path) -> None:
    _database, store = make_store(tmp_path)
    created = create_run(store, tmp_path)

    fetched = store.get(created.id)
    items, total = store.list(limit=10)

    assert fetched.status == "queued"
    assert fetched.request.datasets[0].name == "gsm8k"
    assert fetched.resolved_config.datasets[0].evaluation_version == "v1.1"
    assert fetched.progress == AccuracyRunProgress()
    assert fetched.evalscope_commit == EVALSCOPE_COMMIT
    assert fetched.summary is None
    assert fetched.metrics == []
    assert total == 1
    assert items[0].id == created.id


def test_state_transitions_progress_and_completion_are_persisted(tmp_path: Path) -> None:
    _database, store = make_store(tmp_path)
    run = create_run(store, tmp_path)

    with pytest.raises(AccuracyRunTransitionError, match="queued to running"):
        store.mark_running(run.id, pid=123)

    assert store.mark_preparing(run.id).status == "preparing"
    preparing = store.update_progress(
        run.id,
        AccuracyRunProgress(
            phase="preparing",
            processed_samples=0,
            total_samples=2,
            percent=0,
            message="Preparing EvalScope",
        ),
    )
    assert preparing.progress.total_samples == 2
    running = store.mark_running(run.id, pid=123)
    assert running.status == "running"
    assert running.pid == 123
    assert running.started_at is not None
    running = store.update_progress(
        run.id,
        AccuracyRunProgress(
            phase="running",
            processed_samples=1,
            total_samples=2,
            percent=50,
            current_dataset="gsm8k",
        ),
    )
    assert running.progress.percent == 50
    parsing = store.mark_parsing(run.id)
    assert parsing.status == "parsing"
    assert parsing.pid is None

    completed = store.complete(run.id, make_summary(), [make_metric()])

    assert completed.status == "completed"
    assert completed.progress.percent == 100
    assert completed.summary == make_summary()
    assert completed.metrics == [make_metric()]
    assert completed.finished_at is not None
    assert completed.error is None


def test_partial_completion_requires_matching_incomplete_summary(tmp_path: Path) -> None:
    _database, store = make_store(tmp_path)
    run = create_run(store, tmp_path)
    store.mark_preparing(run.id)
    store.mark_running(run.id, pid=321)
    store.mark_parsing(run.id)
    metric = make_metric(score=0.5).model_copy(
        update={"execution_succeeded": 1, "execution_errored": 1}
    )

    with pytest.raises(ValueError, match="partial status must match"):
        store.complete(run.id, make_summary(incomplete=True), [metric])

    partial = store.complete(run.id, make_summary(incomplete=True), [metric], partial=True)
    assert partial.status == "partial"
    assert partial.summary is not None and partial.summary.incomplete


def test_failure_cancellation_and_missing_run_are_explicit(tmp_path: Path) -> None:
    _database, store = make_store(tmp_path)
    failed_run = create_run(store, tmp_path, name="failed")
    cancelled_run = create_run(store, tmp_path, name="cancelled")

    failed = store.fail(
        failed_run.id,
        AccuracyRunError(
            code="evalscope_failed",
            message="EvalScope exited with code 1",
            retryable=True,
            details={"exit_code": 1},
        ),
    )
    cancelled = store.cancel(cancelled_run.id)

    assert failed.status == "failed"
    assert failed.error is not None and failed.error.details == {"exit_code": 1}
    assert cancelled.status == "cancelled"
    assert cancelled.error is None
    with pytest.raises(AccuracyRunTransitionError):
        store.cancel(cancelled.id)
    with pytest.raises(AccuracyRunNotFound):
        store.get("missing")


def test_recover_incomplete_marks_each_active_status_interrupted(tmp_path: Path) -> None:
    _database, store = make_store(tmp_path)
    queued = create_run(store, tmp_path, name="queued")
    preparing = create_run(store, tmp_path, name="preparing")
    running = create_run(store, tmp_path, name="running")
    parsing = create_run(store, tmp_path, name="parsing")
    terminal = create_run(store, tmp_path, name="terminal")
    store.mark_preparing(preparing.id)
    store.mark_preparing(running.id)
    store.mark_running(running.id, pid=111)
    store.mark_preparing(parsing.id)
    store.mark_running(parsing.id, pid=222)
    store.mark_parsing(parsing.id)
    store.fail(
        terminal.id,
        AccuracyRunError(code="test_failure", message="Already terminal"),
    )

    recovered = store.recover_incomplete()

    assert recovered == 4
    for run_id in (queued.id, preparing.id, running.id, parsing.id):
        item = store.get(run_id)
        assert item.status == "interrupted"
        assert item.progress.phase == "interrupted"
        assert item.pid is None
        assert item.error is not None
        assert item.error.code == "control_service_restarted"
        assert item.error.retryable
    assert store.get(terminal.id).status == "failed"


def test_deleting_run_cascades_normalized_metrics(tmp_path: Path) -> None:
    database, store = make_store(tmp_path)
    run = create_run(store, tmp_path)
    store.mark_preparing(run.id)
    store.mark_running(run.id, pid=100)
    store.mark_parsing(run.id)
    store.complete(run.id, make_summary(), [make_metric()])

    store.delete(run.id)

    with database.connect() as connection:
        metric_count = connection.execute(
            "SELECT COUNT(*) FROM accuracy_run_metrics WHERE run_id = ?", (run.id,)
        ).fetchone()[0]
    assert metric_count == 0
    with pytest.raises(AccuracyRunNotFound):
        store.get(run.id)
