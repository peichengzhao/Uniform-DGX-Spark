from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .database import Database
from .schemas import (
    ArenaLeaderboardReference,
    ArenaProgressRead,
    ArenaResourceSummary,
    ArenaResultSummary,
    ArenaRunCreate,
    ArenaRunRead,
    ArenaSpeculativeMetrics,
)
from .store import to_iso, utc_now


class ArenaRunNotFound(LookupError):
    pass


class ArenaRunStore:
    def __init__(self, database: Database, runs_dir: Path) -> None:
        self.database = database
        self.runs_dir = runs_dir

    def create(
        self,
        request: ArenaRunCreate,
        *,
        target_label: str,
        service_url: str,
        served_model_name: str,
        command_preview: list[str],
    ) -> ArenaRunRead:
        run_id = str(uuid4())
        run_directory = self.runs_dir / run_id
        run_directory.mkdir(parents=True, exist_ok=False)
        now = to_iso(utc_now())
        progress = ArenaProgressRead(
            total_requests=(
                request.runs
                * request.concurrency
                * (2 if request.depth > 0 and request.enable_prefix_caching else 1)
            )
        )
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO arena_runs(
                    id, target_type, target_id, target_label, service_url,
                    served_model_name, status, parameters_json, progress_json,
                    command_preview_json, run_directory, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    request.target_type,
                    request.target_id,
                    target_label,
                    service_url,
                    served_model_name,
                    request.model_dump_json(),
                    progress.model_dump_json(),
                    json.dumps(command_preview, ensure_ascii=False),
                    str(run_directory),
                    now,
                    now,
                ),
            )
            connection.commit()
        return self.get(run_id)

    def get(self, run_id: str) -> ArenaRunRead:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM arena_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise ArenaRunNotFound(run_id)
        return self._from_row(row)

    def list(self, *, limit: int = 100) -> tuple[list[ArenaRunRead], int]:
        with self.database.connect() as connection:
            total = int(connection.execute("SELECT COUNT(*) FROM arena_runs").fetchone()[0])
            rows = connection.execute(
                "SELECT * FROM arena_runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._from_row(row) for row in rows], total

    def mark_running(self, run_id: str) -> ArenaRunRead:
        now = to_iso(utc_now())
        self._update_status(
            run_id,
            "running",
            where="status = 'queued'",
            values={"started_at": now, "updated_at": now},
        )
        return self.get(run_id)

    def update_progress(self, run_id: str, progress: ArenaProgressRead) -> ArenaRunRead:
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE arena_runs SET progress_json = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (progress.model_dump_json(), now, run_id),
            )
            if cursor.rowcount == 0:
                self._require(connection, run_id)
            connection.commit()
        return self.get(run_id)

    def complete(
        self,
        run_id: str,
        summary: ArenaResultSummary,
        reference: ArenaLeaderboardReference | None,
    ) -> ArenaRunRead:
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE arena_runs
                SET status = 'completed', result_summary_json = ?,
                    leaderboard_reference_json = ?, error_message = NULL,
                    finished_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    summary.model_dump_json(),
                    reference.model_dump_json() if reference else None,
                    now,
                    now,
                    run_id,
                ),
            )
            if cursor.rowcount == 0:
                self._require(connection, run_id)
            connection.commit()
        return self.get(run_id)

    def fail(self, run_id: str, message: str) -> ArenaRunRead:
        return self._finish(run_id, "failed", message[:4000])

    def cancel(self, run_id: str) -> ArenaRunRead:
        return self._finish(run_id, "cancelled", None)

    def recover_incomplete(self) -> int:
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE arena_runs
                SET status = 'interrupted',
                    error_message = 'Control service restarted',
                    finished_at = ?, updated_at = ?
                WHERE status IN ('queued', 'running')
                """,
                (now, now),
            )
            connection.commit()
            return cursor.rowcount

    def run_directory(self, run_id: str) -> Path:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT run_directory FROM arena_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise ArenaRunNotFound(run_id)
        return Path(row["run_directory"])

    def delete(self, run_id: str) -> None:
        run_directory = self.run_directory(run_id).resolve()
        root = self.runs_dir.resolve()
        if run_directory == root or root not in run_directory.parents:
            raise RuntimeError("invalid Arena run directory")
        with self.database.connect() as connection:
            cursor = connection.execute("DELETE FROM arena_runs WHERE id = ?", (run_id,))
            if cursor.rowcount == 0:
                raise ArenaRunNotFound(run_id)
            connection.commit()
        if run_directory.is_dir():
            shutil.rmtree(run_directory)

    def _finish(self, run_id: str, status: str, error_message: str | None) -> ArenaRunRead:
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE arena_runs
                SET status = ?, error_message = ?, finished_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('queued', 'running')
                """,
                (status, error_message, now, now, run_id),
            )
            if cursor.rowcount == 0:
                self._require(connection, run_id)
            connection.commit()
        return self.get(run_id)

    def _update_status(
        self,
        run_id: str,
        status: str,
        *,
        where: str,
        values: dict[str, str],
    ) -> None:
        assignments = ["status = ?", *[f"{key} = ?" for key in values]]
        parameters = [status, *values.values(), run_id]
        with self.database.connect() as connection:
            cursor = connection.execute(
                f"UPDATE arena_runs SET {', '.join(assignments)} WHERE id = ? AND {where}",
                parameters,
            )
            if cursor.rowcount == 0:
                self._require(connection, run_id)
            connection.commit()

    @staticmethod
    def _require(connection: object, run_id: str) -> None:
        row = connection.execute(  # type: ignore[attr-defined]
            "SELECT 1 FROM arena_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise ArenaRunNotFound(run_id)

    @staticmethod
    def _from_row(row: object) -> ArenaRunRead:
        values = dict(row)  # type: ignore[arg-type]
        run_directory = Path(values["run_directory"])
        return ArenaRunRead(
            id=values["id"],
            target_type=values["target_type"],
            target_id=values["target_id"],
            target_label=values["target_label"],
            service_url=values["service_url"],
            served_model_name=values["served_model_name"],
            status=values["status"],
            parameters=ArenaRunCreate.model_validate_json(values["parameters_json"]),
            progress=ArenaProgressRead.model_validate_json(values["progress_json"]),
            result_summary=(
                ArenaResultSummary.model_validate_json(values["result_summary_json"])
                if values["result_summary_json"]
                else None
            ),
            speculative_metrics=ArenaRunStore._optional_file_model(
                run_directory / "speculative-metrics.json",
                ArenaSpeculativeMetrics,
            ),
            resource_summary=ArenaRunStore._optional_file_model(
                run_directory / "resource-summary.json",
                ArenaResourceSummary,
            ),
            leaderboard_reference=(
                ArenaLeaderboardReference.model_validate_json(values["leaderboard_reference_json"])
                if values["leaderboard_reference_json"]
                else None
            ),
            command_preview=json.loads(values["command_preview_json"]),
            result_available=(run_directory / "result.json").is_file(),
            error_message=values["error_message"],
            created_at=ArenaRunStore._datetime(values["created_at"]),
            started_at=ArenaRunStore._optional_datetime(values["started_at"]),
            finished_at=ArenaRunStore._optional_datetime(values["finished_at"]),
            updated_at=ArenaRunStore._datetime(values["updated_at"]),
        )

    @staticmethod
    def _datetime(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    @staticmethod
    def _optional_datetime(value: str | None) -> datetime | None:
        return ArenaRunStore._datetime(value) if value else None

    @staticmethod
    def _optional_file_model(path: Path, model: object) -> object | None:
        if not path.is_file():
            return None
        try:
            return model.model_validate_json(path.read_text(encoding="utf-8"))  # type: ignore[attr-defined]
        except (OSError, ValueError):
            return None
