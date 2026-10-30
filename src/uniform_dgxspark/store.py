from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .database import Database
from .schemas import (
    DeploymentCreate,
    DeploymentEventRead,
    DeploymentParameters,
    DeploymentRead,
)
from .state import (
    RECOVERABLE_STATUSES,
    TERMINAL_STATUSES,
    DeploymentStatus,
    InvalidTransition,
    ensure_transition_allowed,
)


class DeploymentNotFound(LookupError):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class DeploymentStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, request: DeploymentCreate) -> DeploymentRead:
        deployment_id = str(uuid4())
        now = to_iso(utc_now())
        parameters_json = request.parameters.model_dump_json()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO deployments(
                    id, model_id, revision, display_name, parameters_json,
                    status, progress, attempt, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, 1, ?, ?)
                """,
                (
                    deployment_id,
                    request.model_id,
                    request.revision,
                    request.display_name,
                    parameters_json,
                    DeploymentStatus.QUEUED,
                    now,
                    now,
                ),
            )
            self._insert_event(
                connection,
                deployment_id=deployment_id,
                event_type="created",
                from_status=None,
                to_status=DeploymentStatus.QUEUED,
                message="Deployment queued",
                details={},
                created_at=now,
            )
            connection.commit()
        return self.get(deployment_id)

    def get(self, deployment_id: str) -> DeploymentRead:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM deployments WHERE id = ?", (deployment_id,)
            ).fetchone()
        if row is None:
            raise DeploymentNotFound(deployment_id)
        return self._deployment_from_row(row)

    def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        include_archived: bool = False,
    ) -> tuple[list[DeploymentRead], int]:
        where = "" if include_archived else "WHERE archived_at IS NULL"
        with self.database.connect() as connection:
            total = int(
                connection.execute(f"SELECT COUNT(*) FROM deployments {where}").fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT * FROM deployments {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [self._deployment_from_row(row) for row in rows], total

    def list_by_status(
        self, statuses: list[DeploymentStatus], *, limit: int = 50
    ) -> list[DeploymentRead]:
        if not statuses:
            return []
        values = tuple(status.value for status in statuses)
        placeholders = ",".join("?" for _ in values)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM deployments
                WHERE archived_at IS NULL AND status IN ({placeholders})
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (*values, limit),
            ).fetchall()
        return [self._deployment_from_row(row) for row in rows]

    def archive(self, deployment_id: str) -> DeploymentRead:
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, archived_at FROM deployments WHERE id = ?",
                (deployment_id,),
            ).fetchone()
            if row is None:
                raise DeploymentNotFound(deployment_id)
            if row["archived_at"] is not None:
                connection.commit()
                return self.get(deployment_id)
            current = DeploymentStatus(row["status"])
            if current not in TERMINAL_STATUSES:
                raise InvalidTransition(
                    f"cannot archive deployment while status is {current.value}"
                )
            connection.execute(
                """
                UPDATE deployments
                SET archived_at = ?, version = version + 1, updated_at = ?
                WHERE id = ?
                """,
                (now, now, deployment_id),
            )
            self._insert_event(
                connection,
                deployment_id=deployment_id,
                event_type="archived",
                from_status=None,
                to_status=None,
                message="Deployment archived by user",
                details={},
                created_at=now,
            )
            connection.commit()
        return self.get(deployment_id)

    def restore(self, deployment_id: str) -> DeploymentRead:
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT archived_at FROM deployments WHERE id = ?", (deployment_id,)
            ).fetchone()
            if row is None:
                raise DeploymentNotFound(deployment_id)
            if row["archived_at"] is None:
                connection.commit()
                return self.get(deployment_id)
            connection.execute(
                """
                UPDATE deployments
                SET archived_at = NULL, version = version + 1, updated_at = ?
                WHERE id = ?
                """,
                (now, deployment_id),
            )
            self._insert_event(
                connection,
                deployment_id=deployment_id,
                event_type="restored",
                from_status=None,
                to_status=None,
                message="Deployment restored from archive",
                details={},
                created_at=now,
            )
            connection.commit()
        return self.get(deployment_id)

    def update_progress(self, deployment_id: str, progress: float) -> DeploymentRead:
        if not 0 <= progress <= 1:
            raise ValueError("progress must be between 0 and 1")
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE deployments
                SET progress = ?, version = version + 1, updated_at = ?
                WHERE id = ?
                """,
                (progress, now, deployment_id),
            )
            if cursor.rowcount == 0:
                raise DeploymentNotFound(deployment_id)
            connection.commit()
        return self.get(deployment_id)

    def set_revision(self, deployment_id: str, revision: str) -> DeploymentRead:
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision FROM deployments WHERE id = ?", (deployment_id,)
            ).fetchone()
            if row is None:
                raise DeploymentNotFound(deployment_id)
            if row["revision"] == revision:
                connection.commit()
                return self.get(deployment_id)
            connection.execute(
                """
                UPDATE deployments
                SET revision = ?, version = version + 1, updated_at = ?
                WHERE id = ?
                """,
                (revision, now, deployment_id),
            )
            self._insert_event(
                connection,
                deployment_id=deployment_id,
                event_type="revision_resolved",
                from_status=None,
                to_status=None,
                message=f"Resolved immutable model revision {revision}",
                details={"revision": revision},
                created_at=now,
            )
            connection.commit()
        return self.get(deployment_id)

    def list_events(self, deployment_id: str) -> list[DeploymentEventRead]:
        self.get(deployment_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM deployment_events
                WHERE deployment_id = ?
                ORDER BY sequence ASC
                """,
                (deployment_id,),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def record_event(
        self,
        deployment_id: str,
        event_type: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.get(deployment_id)
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._insert_event(
                connection,
                deployment_id=deployment_id,
                event_type=event_type,
                from_status=None,
                to_status=None,
                message=message,
                details=details or {},
                created_at=now,
            )
            connection.commit()

    def cancel(self, deployment_id: str) -> DeploymentRead:
        current = self.get(deployment_id)
        if current.status == DeploymentStatus.CANCELLED:
            return current
        if current.status in TERMINAL_STATUSES:
            return current
        return self.transition(
            deployment_id,
            DeploymentStatus.CANCELLED,
            message="Deployment cancelled by user",
            event_type="cancelled",
        )

    def transition(
        self,
        deployment_id: str,
        target: DeploymentStatus,
        *,
        message: str,
        event_type: str = "status_changed",
        progress: float | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> DeploymentRead:
        if progress is not None and not 0 <= progress <= 1:
            raise ValueError("progress must be between 0 and 1")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM deployments WHERE id = ?", (deployment_id,)
            ).fetchone()
            if row is None:
                raise DeploymentNotFound(deployment_id)
            current = DeploymentStatus(row["status"])
            ensure_transition_allowed(current, target)
            now = to_iso(utc_now())
            next_progress = float(row["progress"]) if progress is None else progress
            connection.execute(
                """
                UPDATE deployments
                SET status = ?, progress = ?, error_code = ?, error_message = ?,
                    version = version + 1, updated_at = ?
                WHERE id = ?
                """,
                (
                    target,
                    next_progress,
                    error_code,
                    error_message,
                    now,
                    deployment_id,
                ),
            )
            self._insert_event(
                connection,
                deployment_id=deployment_id,
                event_type=event_type,
                from_status=current,
                to_status=target,
                message=message,
                details=details or {},
                created_at=now,
            )
            connection.commit()
        return self.get(deployment_id)

    def recover_incomplete(self) -> int:
        recoverable_values = tuple(status.value for status in RECOVERABLE_STATUSES)
        placeholders = ",".join("?" for _ in recoverable_values)
        recovered = 0
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"SELECT * FROM deployments WHERE status IN ({placeholders})",
                recoverable_values,
            ).fetchall()
            for row in rows:
                deployment_id = str(row["id"])
                previous = DeploymentStatus(row["status"])
                now = to_iso(utc_now())
                connection.execute(
                    """
                    UPDATE deployments
                    SET status = ?, progress = 0, attempt = attempt + 1,
                        error_code = NULL, error_message = NULL,
                        version = version + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (DeploymentStatus.QUEUED, now, deployment_id),
                )
                self._insert_event(
                    connection,
                    deployment_id=deployment_id,
                    event_type="recovered",
                    from_status=previous,
                    to_status=DeploymentStatus.QUEUED,
                    message="Interrupted deployment re-queued during startup",
                    details={"previous_status": previous.value},
                    created_at=now,
                )
                recovered += 1
            connection.commit()
        return recovered

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        deployment_id: str,
        event_type: str,
        from_status: DeploymentStatus | None,
        to_status: DeploymentStatus | None,
        message: str,
        details: dict[str, Any],
        created_at: str,
    ) -> None:
        sequence = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(sequence), 0) + 1
                FROM deployment_events WHERE deployment_id = ?
                """,
                (deployment_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO deployment_events(
                deployment_id, sequence, event_type, from_status, to_status,
                message, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                deployment_id,
                sequence,
                event_type,
                from_status.value if from_status else None,
                to_status.value if to_status else None,
                message,
                json.dumps(details, separators=(",", ":"), sort_keys=True),
                created_at,
            ),
        )

    @staticmethod
    def _deployment_from_row(row: sqlite3.Row) -> DeploymentRead:
        return DeploymentRead(
            id=row["id"],
            model_id=row["model_id"],
            revision=row["revision"],
            display_name=row["display_name"],
            parameters=DeploymentParameters.model_validate_json(row["parameters_json"]),
            status=DeploymentStatus(row["status"]),
            progress=float(row["progress"]),
            error_code=row["error_code"],
            error_message=row["error_message"],
            attempt=int(row["attempt"]),
            version=int(row["version"]),
            archived_at=(
                datetime.fromisoformat(row["archived_at"].replace("Z", "+00:00"))
                if row["archived_at"]
                else None
            ),
            created_at=datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")),
            updated_at=datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00")),
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> DeploymentEventRead:
        return DeploymentEventRead(
            id=int(row["id"]),
            deployment_id=row["deployment_id"],
            sequence=int(row["sequence"]),
            event_type=row["event_type"],
            from_status=(DeploymentStatus(row["from_status"]) if row["from_status"] else None),
            to_status=(DeploymentStatus(row["to_status"]) if row["to_status"] else None),
            message=row["message"],
            details=json.loads(row["details_json"]),
            created_at=datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")),
        )
