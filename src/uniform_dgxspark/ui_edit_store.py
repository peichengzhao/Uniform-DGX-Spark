from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from .database import Database
from .schemas import UIEditCreate, UIEditRead, UIEditTarget
from .store import to_iso, utc_now


class UIEditNotFound(LookupError):
    pass


class UIEditStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, job_id: str, request: UIEditCreate, workspace_path: str) -> UIEditRead:
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO ui_edit_jobs(
                    id, provider, status, instruction, scope, target_json,
                    workspace_path, created_at, updated_at
                ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    request.provider,
                    request.instruction,
                    request.scope,
                    request.target.model_dump_json(),
                    workspace_path,
                    now,
                    now,
                ),
            )
            connection.commit()
        return self.get(job_id)

    def get(self, job_id: str) -> UIEditRead:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM ui_edit_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise UIEditNotFound(job_id)
        return self._from_row(row)

    def list(self, *, limit: int = 20) -> tuple[list[UIEditRead], int]:
        with self.database.connect() as connection:
            total = int(connection.execute("SELECT COUNT(*) FROM ui_edit_jobs").fetchone()[0])
            rows = connection.execute(
                "SELECT * FROM ui_edit_jobs ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._from_row(row) for row in rows], total

    def update(
        self,
        job_id: str,
        status: str,
        *,
        changed_files: list[str] | None = None,
        summary: str | None = None,
        error_message: str | None = None,
        base_hashes: dict[str, str] | None = None,
        applied_hashes: dict[str, str] | None = None,
        warnings: list[str] | None = None,
        codex_session_id: str | None = None,
        commit_sha: str | None = None,
        revert_commit_sha: str | None = None,
        rollback_reason: str | None = None,
    ) -> UIEditRead:
        assignments = ["status = ?", "updated_at = ?"]
        values: list[Any] = [status, to_iso(utc_now())]
        optional = {
            "changed_files_json": (
                json.dumps(changed_files, ensure_ascii=False) if changed_files is not None else None
            ),
            "summary": summary,
            "error_message": error_message,
            "base_hashes_json": (
                json.dumps(base_hashes, ensure_ascii=False) if base_hashes is not None else None
            ),
            "applied_hashes_json": (
                json.dumps(applied_hashes, ensure_ascii=False)
                if applied_hashes is not None
                else None
            ),
            "warnings_json": (
                json.dumps(warnings, ensure_ascii=False) if warnings is not None else None
            ),
            "codex_session_id": codex_session_id,
            "commit_sha": commit_sha,
            "revert_commit_sha": revert_commit_sha,
            "rollback_reason": rollback_reason,
        }
        for column, value in optional.items():
            if value is not None:
                assignments.append(f"{column} = ?")
                values.append(value)
        values.append(job_id)
        with self.database.connect() as connection:
            cursor = connection.execute(
                f"UPDATE ui_edit_jobs SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
            if cursor.rowcount == 0:
                raise UIEditNotFound(job_id)
            connection.commit()
        return self.get(job_id)

    def recovery_fail_incomplete(self) -> int:
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE ui_edit_jobs
                SET status = 'failed', error_message = ?, updated_at = ?
                WHERE status IN ('queued', 'running', 'repairing', 'validating')
                """,
                ("UI edit interrupted by service restart", now),
            )
            connection.commit()
        return cursor.rowcount

    def latest_applied(self) -> UIEditRead | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM ui_edit_jobs
                WHERE status IN ('applied', 'applied_with_warnings')
                ORDER BY updated_at DESC, id DESC LIMIT 1
                """
            ).fetchone()
        return self._from_row(row) if row else None

    def metadata(self, job_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT workspace_path, base_hashes_json, applied_hashes_json,
                       codex_session_id, commit_sha, revert_commit_sha, rollback_reason,
                       warnings_json
                FROM ui_edit_jobs WHERE id = ?
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            raise UIEditNotFound(job_id)
        return {
            "workspace_path": row["workspace_path"],
            "base_hashes": json.loads(row["base_hashes_json"]),
            "applied_hashes": json.loads(row["applied_hashes_json"]),
            "codex_session_id": row["codex_session_id"],
            "commit_sha": row["commit_sha"],
            "revert_commit_sha": row["revert_commit_sha"],
            "rollback_reason": row["rollback_reason"],
            "warnings": json.loads(row["warnings_json"]),
        }

    def rollback_feedback(self, *, limit: int = 10) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT instruction, scope, target_json, summary, rollback_reason
                FROM ui_edit_jobs
                WHERE status = 'reverted' AND rollback_reason IS NOT NULL
                ORDER BY updated_at DESC, id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "instruction": row["instruction"],
                "scope": row["scope"],
                "target": {
                    key: value
                    for key, value in json.loads(row["target_json"]).items()
                    if key in {"ui_id", "dom_path", "view_id"}
                },
                "summary": row["summary"],
                "rollback_reason": row["rollback_reason"],
            }
            for row in rows
        ]

    @staticmethod
    def _from_row(row: sqlite3.Row) -> UIEditRead:
        return UIEditRead(
            id=row["id"],
            provider=row["provider"],
            status=row["status"],
            instruction=row["instruction"],
            scope=row["scope"],
            target=UIEditTarget.model_validate_json(row["target_json"]),
            changed_files=json.loads(row["changed_files_json"]),
            summary=row["summary"],
            error_message=row["error_message"],
            warnings=json.loads(row["warnings_json"]),
            codex_session_id=row["codex_session_id"],
            commit_sha=row["commit_sha"],
            revert_commit_sha=row["revert_commit_sha"],
            rollback_reason=row["rollback_reason"],
            created_at=datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")),
            updated_at=datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00")),
        )
