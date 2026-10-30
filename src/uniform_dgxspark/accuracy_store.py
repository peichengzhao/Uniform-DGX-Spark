from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .accuracy_identity import cache_fingerprint, evaluation_fingerprint
from .database import Database
from .schemas import (
    AccuracyMetricRead,
    AccuracyResolvedConfig,
    AccuracyRunCreate,
    AccuracyRunError,
    AccuracyRunProgress,
    AccuracyRunRead,
    AccuracyRunStatus,
    AccuracyRunSummary,
)
from .store import to_iso, utc_now


class AccuracyRunNotFound(LookupError):
    pass


class AccuracyRunTransitionError(ValueError):
    pass


_ACTIVE_STATUSES = {"queued", "preparing", "running", "parsing"}


class AccuracyRunStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        request: AccuracyRunCreate,
        *,
        target_label: str,
        model_name: str,
        service_url: str,
        resolved_config: AccuracyResolvedConfig,
        evalscope_version: str,
        evalscope_commit: str | None,
        run_directory: Path,
    ) -> AccuracyRunRead:
        run_id = str(uuid4())
        now = to_iso(utc_now())
        progress = AccuracyRunProgress()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO accuracy_runs(
                    id, target_type, target_id, target_label, model_name, service_url,
                    status, request_json, resolved_config_json, progress_json,
                    evalscope_version, evalscope_commit, run_directory, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    request.target_type,
                    request.target_id,
                    target_label,
                    model_name,
                    service_url,
                    request.model_dump_json(),
                    resolved_config.model_dump_json(),
                    progress.model_dump_json(),
                    evalscope_version,
                    evalscope_commit,
                    str(run_directory.resolve()),
                    now,
                    now,
                ),
            )
            fingerprint = evaluation_fingerprint(
                resolved_config, evalscope_version, evalscope_commit
            )
            connection.execute(
                "UPDATE accuracy_runs SET evaluation_fingerprint = ?, cache_fingerprint = ? "
                "WHERE id = ?",
                (
                    fingerprint,
                    cache_fingerprint(fingerprint, model_name, resolved_config.deployment_snapshot),
                    run_id,
                ),
            )
            connection.commit()
        return self.get(run_id)

    def get(self, run_id: str) -> AccuracyRunRead:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM accuracy_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise AccuracyRunNotFound(run_id)
            metrics = self._metrics(connection, run_id)
        return self._from_row(row, metrics)

    def list(self, *, limit: int = 100, offset: int = 0) -> tuple[list[AccuracyRunRead], int]:
        with self.database.connect() as connection:
            total = int(connection.execute("SELECT COUNT(*) FROM accuracy_runs").fetchone()[0])
            rows = connection.execute(
                """
                SELECT * FROM accuracy_runs
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
            items = [self._from_row(row, self._metrics(connection, row["id"])) for row in rows]
        return items, total

    def mark_preparing(self, run_id: str) -> AccuracyRunRead:
        return self._transition(run_id, "preparing", {"queued"})

    def set_cached_samples(self, run_id: str, count: int) -> None:
        if count < 0:
            raise ValueError("cached samples must be nonnegative")
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE accuracy_runs SET cached_samples = ? WHERE id = ?", (count, run_id)
            )
            connection.commit()

    def mark_running(self, run_id: str, *, pid: int) -> AccuracyRunRead:
        if pid <= 0:
            raise ValueError("pid must be positive")
        return self._transition(run_id, "running", {"preparing"}, pid=pid, mark_started=True)

    def mark_parsing(self, run_id: str) -> AccuracyRunRead:
        return self._transition(run_id, "parsing", {"running"}, pid=None)

    def update_pid(self, run_id: str, *, pid: int) -> AccuracyRunRead:
        if pid <= 0:
            raise ValueError("pid must be positive")
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require(connection, run_id)
            if row["status"] != "running":
                raise AccuracyRunTransitionError(
                    f"cannot update pid while status is {row['status']}"
                )
            connection.execute(
                "UPDATE accuracy_runs SET pid = ?, updated_at = ? WHERE id = ?",
                (pid, now, run_id),
            )
            connection.commit()
        return self.get(run_id)

    def update_progress(self, run_id: str, progress: AccuracyRunProgress) -> AccuracyRunRead:
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require(connection, run_id)
            current = str(row["status"])
            if current not in {"preparing", "running", "parsing"}:
                raise AccuracyRunTransitionError(
                    f"cannot update progress while status is {current}"
                )
            if progress.phase != current:
                raise AccuracyRunTransitionError(
                    f"progress phase {progress.phase} does not match run status {current}"
                )
            connection.execute(
                "UPDATE accuracy_runs SET progress_json = ?, updated_at = ? WHERE id = ?",
                (progress.model_dump_json(), now, run_id),
            )
            connection.commit()
        return self.get(run_id)

    def complete(
        self,
        run_id: str,
        summary: AccuracyRunSummary,
        metrics: list[AccuracyMetricRead],
        *,
        partial: bool = False,
    ) -> AccuracyRunRead:
        if not metrics:
            raise ValueError("completed accuracy runs require at least one metric")
        if summary.metric_count != len(metrics):
            raise ValueError("summary.metric_count must match the number of metrics")
        dataset_count = len({metric.dataset_name for metric in metrics})
        if summary.dataset_count != dataset_count:
            raise ValueError("summary.dataset_count must match metric datasets")
        if summary.primary_metric_count != sum(metric.is_primary for metric in metrics):
            raise ValueError("summary.primary_metric_count must match primary metrics")
        if summary.incomplete != partial:
            raise ValueError("partial status must match summary.incomplete")
        target_status = "partial" if partial else "completed"
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require(connection, run_id)
            if row["status"] != "parsing":
                raise AccuracyRunTransitionError(
                    f"cannot transition from {row['status']} to {target_status}"
                )
            connection.execute("DELETE FROM accuracy_run_metrics WHERE run_id = ?", (run_id,))
            connection.executemany(
                """
                INSERT INTO accuracy_run_metrics(
                    run_id, dataset_name, dataset_pretty_name, evaluation_version,
                    metric_key, metric_display_name, metric_kind, higher_is_better,
                    category_path_json, subset_name, sample_count, score, is_primary,
                    execution_requested, execution_succeeded, execution_errored
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [self._metric_values(run_id, metric) for metric in metrics],
            )
            progress = AccuracyRunProgress(
                phase=target_status,
                processed_samples=summary.execution_succeeded + summary.execution_errored,
                total_samples=summary.execution_requested,
                percent=100,
                message="Evaluation completed with incomplete results"
                if partial
                else "Evaluation completed",
            )
            connection.execute(
                """
                UPDATE accuracy_runs
                SET status = ?, progress_json = ?, summary_json = ?, pid = NULL,
                    error_code = NULL, error_message = NULL, error_retryable = NULL,
                    error_details_json = NULL, finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    target_status,
                    progress.model_dump_json(),
                    summary.model_dump_json(),
                    now,
                    now,
                    run_id,
                ),
            )
            connection.commit()
        return self.get(run_id)

    def fail(self, run_id: str, error: AccuracyRunError) -> AccuracyRunRead:
        return self._finish(run_id, "failed", error)

    def cancel(self, run_id: str) -> AccuracyRunRead:
        return self._finish(run_id, "cancelled", None)

    def recover_incomplete(self) -> int:
        now = to_iso(utc_now())
        error = AccuracyRunError(
            code="control_service_restarted",
            message="Control service restarted before the evaluation finished",
            retryable=True,
        )
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id, progress_json FROM accuracy_runs WHERE status IN (?, ?, ?, ?)",
                tuple(sorted(_ACTIVE_STATUSES)),
            ).fetchall()
            for row in rows:
                progress = AccuracyRunProgress.model_validate_json(row["progress_json"])
                interrupted = progress.model_copy(
                    update={"phase": "interrupted", "message": error.message}
                )
                connection.execute(
                    """
                    UPDATE accuracy_runs
                    SET status = 'interrupted', progress_json = ?, pid = NULL,
                        error_code = ?, error_message = ?, error_retryable = ?,
                        error_details_json = ?, finished_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        interrupted.model_dump_json(),
                        error.code,
                        error.message,
                        int(error.retryable),
                        json.dumps(error.details, ensure_ascii=False),
                        now,
                        now,
                        row["id"],
                    ),
                )
            connection.commit()
        return len(rows)

    def delete(self, run_id: str) -> None:
        with self.database.connect() as connection:
            cursor = connection.execute("DELETE FROM accuracy_runs WHERE id = ?", (run_id,))
            if cursor.rowcount == 0:
                raise AccuracyRunNotFound(run_id)
            connection.commit()

    def _transition(
        self,
        run_id: str,
        target: AccuracyRunStatus,
        allowed_from: set[str],
        *,
        pid: int | None = None,
        mark_started: bool = False,
    ) -> AccuracyRunRead:
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require(connection, run_id)
            current = str(row["status"])
            if current not in allowed_from:
                raise AccuracyRunTransitionError(f"cannot transition from {current} to {target}")
            progress = AccuracyRunProgress.model_validate_json(row["progress_json"])
            progress = progress.model_copy(update={"phase": target})
            started_at = now if mark_started else row["started_at"]
            connection.execute(
                """
                UPDATE accuracy_runs
                SET status = ?, progress_json = ?, pid = ?, started_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (target, progress.model_dump_json(), pid, started_at, now, run_id),
            )
            connection.commit()
        return self.get(run_id)

    def _finish(
        self,
        run_id: str,
        status: AccuracyRunStatus,
        error: AccuracyRunError | None,
    ) -> AccuracyRunRead:
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require(connection, run_id)
            current = str(row["status"])
            if current not in _ACTIVE_STATUSES:
                raise AccuracyRunTransitionError(f"cannot transition from {current} to {status}")
            progress = AccuracyRunProgress.model_validate_json(row["progress_json"])
            progress = progress.model_copy(
                update={
                    "phase": status,
                    "message": error.message if error else "Evaluation cancelled",
                }
            )
            connection.execute(
                """
                UPDATE accuracy_runs
                SET status = ?, progress_json = ?, pid = NULL, error_code = ?,
                    error_message = ?, error_retryable = ?, error_details_json = ?,
                    finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    progress.model_dump_json(),
                    error.code if error else None,
                    error.message if error else None,
                    int(error.retryable) if error else None,
                    json.dumps(error.details, ensure_ascii=False) if error else None,
                    now,
                    now,
                    run_id,
                ),
            )
            connection.commit()
        return self.get(run_id)

    @staticmethod
    def _require(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM accuracy_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise AccuracyRunNotFound(run_id)
        return row

    @staticmethod
    def _metric_values(run_id: str, metric: AccuracyMetricRead) -> tuple[object, ...]:
        return (
            run_id,
            metric.dataset_name,
            metric.dataset_pretty_name,
            metric.evaluation_version,
            metric.metric_key,
            metric.metric_display_name,
            metric.metric_kind,
            int(metric.higher_is_better),
            json.dumps(metric.category_path, ensure_ascii=False, separators=(",", ":")),
            metric.subset_name,
            metric.sample_count,
            metric.score,
            int(metric.is_primary),
            metric.execution_requested,
            metric.execution_succeeded,
            metric.execution_errored,
        )

    @staticmethod
    def _metrics(connection: sqlite3.Connection, run_id: str) -> list[AccuracyMetricRead]:
        rows = connection.execute(
            """
            SELECT * FROM accuracy_run_metrics
            WHERE run_id = ?
            ORDER BY dataset_name, is_primary DESC, metric_key, category_path_json, subset_name
            """,
            (run_id,),
        ).fetchall()
        return [
            AccuracyMetricRead(
                dataset_name=row["dataset_name"],
                dataset_pretty_name=row["dataset_pretty_name"],
                evaluation_version=row["evaluation_version"],
                metric_key=row["metric_key"],
                metric_display_name=row["metric_display_name"],
                metric_kind=row["metric_kind"],
                higher_is_better=bool(row["higher_is_better"]),
                category_path=json.loads(row["category_path_json"]),
                subset_name=row["subset_name"],
                sample_count=int(row["sample_count"]),
                score=float(row["score"]),
                is_primary=bool(row["is_primary"]),
                execution_requested=int(row["execution_requested"]),
                execution_succeeded=int(row["execution_succeeded"]),
                execution_errored=int(row["execution_errored"]),
            )
            for row in rows
        ]

    @staticmethod
    def _from_row(row: sqlite3.Row, metrics: list[AccuracyMetricRead]) -> AccuracyRunRead:
        error = None
        if row["error_code"] is not None:
            error = AccuracyRunError(
                code=row["error_code"],
                message=row["error_message"],
                retryable=bool(row["error_retryable"]),
                details=json.loads(row["error_details_json"] or "{}"),
            )
        return AccuracyRunRead(
            id=row["id"],
            target_type=row["target_type"],
            target_id=row["target_id"],
            target_label=row["target_label"],
            model_name=row["model_name"],
            service_url=row["service_url"],
            status=row["status"],
            request=AccuracyRunCreate.model_validate_json(row["request_json"]),
            resolved_config=AccuracyResolvedConfig.model_validate_json(row["resolved_config_json"]),
            progress=AccuracyRunProgress.model_validate_json(row["progress_json"]),
            summary=(
                AccuracyRunSummary.model_validate_json(row["summary_json"])
                if row["summary_json"]
                else None
            ),
            metrics=metrics,
            evalscope_version=row["evalscope_version"],
            evalscope_commit=row["evalscope_commit"],
            run_directory=row["run_directory"],
            pid=int(row["pid"]) if row["pid"] is not None else None,
            error=error,
            created_at=AccuracyRunStore._datetime(row["created_at"]),
            started_at=AccuracyRunStore._optional_datetime(row["started_at"]),
            finished_at=AccuracyRunStore._optional_datetime(row["finished_at"]),
            updated_at=AccuracyRunStore._datetime(row["updated_at"]),
            evaluation_fingerprint=row["evaluation_fingerprint"],
            cache_fingerprint=row["cache_fingerprint"],
            cached_samples=row["cached_samples"],
        )

    @staticmethod
    def _datetime(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    @staticmethod
    def _optional_datetime(value: str | None) -> datetime | None:
        return AccuracyRunStore._datetime(value) if value else None
