from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from .database import Database
from .schemas import BenchmarkCreate, BenchmarkMetrics, BenchmarkRead
from .store import to_iso, utc_now


class BenchmarkNotFound(LookupError):
    pass


class BenchmarkStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        request: BenchmarkCreate,
        *,
        target_label: str,
        model_name: str,
        service_url: str,
    ) -> BenchmarkRead:
        benchmark_id = str(uuid4())
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO benchmarks(
                    id, target_type, target_id, target_label, model_name, service_url,
                    status, parameters_json, progress, completed_requests,
                    successful_requests, failed_requests, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, 0, 0, 0, 0, ?, ?)
                """,
                (
                    benchmark_id,
                    request.target_type,
                    request.target_id,
                    target_label,
                    model_name,
                    service_url,
                    request.model_dump_json(),
                    now,
                    now,
                ),
            )
            connection.commit()
        return self.get(benchmark_id)

    def get(self, benchmark_id: str) -> BenchmarkRead:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM benchmarks WHERE id = ?", (benchmark_id,)
            ).fetchone()
        if row is None:
            raise BenchmarkNotFound(benchmark_id)
        return self._from_row(row)

    def list(self, *, limit: int = 100) -> tuple[list[BenchmarkRead], int]:
        with self.database.connect() as connection:
            total = int(connection.execute("SELECT COUNT(*) FROM benchmarks").fetchone()[0])
            rows = connection.execute(
                "SELECT * FROM benchmarks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._from_row(row) for row in rows], total

    def mark_running(self, benchmark_id: str) -> BenchmarkRead:
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE benchmarks
                SET status = 'running', started_at = ?, updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (now, now, benchmark_id),
            )
            if cursor.rowcount == 0:
                self._require(connection, benchmark_id)
            connection.commit()
        return self.get(benchmark_id)

    def update_progress(
        self,
        benchmark_id: str,
        *,
        completed_requests: int,
        successful_requests: int,
        failed_requests: int,
        total_requests: int,
    ) -> BenchmarkRead:
        progress = min(1.0, completed_requests / max(1, total_requests))
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE benchmarks
                SET progress = ?, completed_requests = ?, successful_requests = ?,
                    failed_requests = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    progress,
                    completed_requests,
                    successful_requests,
                    failed_requests,
                    now,
                    benchmark_id,
                ),
            )
            if cursor.rowcount == 0:
                self._require(connection, benchmark_id)
            connection.commit()
        return self.get(benchmark_id)

    def complete(self, benchmark_id: str, metrics: BenchmarkMetrics) -> BenchmarkRead:
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE benchmarks
                SET status = 'completed', progress = 1, metrics_json = ?,
                    error_message = NULL, finished_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (metrics.model_dump_json(), now, now, benchmark_id),
            )
            if cursor.rowcount == 0:
                self._require(connection, benchmark_id)
            connection.commit()
        return self.get(benchmark_id)

    def fail(self, benchmark_id: str, message: str) -> BenchmarkRead:
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE benchmarks
                SET status = 'failed', error_message = ?, finished_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('queued', 'running')
                """,
                (message[:2000], now, now, benchmark_id),
            )
            if cursor.rowcount == 0:
                self._require(connection, benchmark_id)
            connection.commit()
        return self.get(benchmark_id)

    def cancel(self, benchmark_id: str) -> BenchmarkRead:
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE benchmarks
                SET status = 'cancelled', finished_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('queued', 'running')
                """,
                (now, now, benchmark_id),
            )
            if cursor.rowcount == 0:
                self._require(connection, benchmark_id)
            connection.commit()
        return self.get(benchmark_id)

    def recover_incomplete(self) -> int:
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE benchmarks
                SET status = 'interrupted', error_message = 'Control service restarted',
                    finished_at = ?, updated_at = ?
                WHERE status IN ('queued', 'running')
                """,
                (now, now),
            )
            connection.commit()
            return cursor.rowcount

    @staticmethod
    def _require(connection: object, benchmark_id: str) -> None:
        row = connection.execute(  # type: ignore[attr-defined]
            "SELECT 1 FROM benchmarks WHERE id = ?", (benchmark_id,)
        ).fetchone()
        if row is None:
            raise BenchmarkNotFound(benchmark_id)

    @staticmethod
    def _from_row(row: object) -> BenchmarkRead:
        values = dict(row)  # type: ignore[arg-type]
        return BenchmarkRead(
            id=values["id"],
            target_type=values["target_type"],
            target_id=values["target_id"],
            target_label=values["target_label"],
            model_name=values["model_name"],
            service_url=values["service_url"],
            status=values["status"],
            parameters=BenchmarkCreate.model_validate_json(values["parameters_json"]),
            progress=float(values["progress"]),
            completed_requests=int(values["completed_requests"]),
            successful_requests=int(values["successful_requests"]),
            failed_requests=int(values["failed_requests"]),
            metrics=(
                BenchmarkMetrics.model_validate_json(values["metrics_json"])
                if values["metrics_json"]
                else None
            ),
            error_message=values["error_message"],
            created_at=BenchmarkStore._datetime(values["created_at"]),
            started_at=BenchmarkStore._optional_datetime(values["started_at"]),
            finished_at=BenchmarkStore._optional_datetime(values["finished_at"]),
            updated_at=BenchmarkStore._datetime(values["updated_at"]),
        )

    @staticmethod
    def _datetime(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    @staticmethod
    def _optional_datetime(value: str | None) -> datetime | None:
        return BenchmarkStore._datetime(value) if value else None
