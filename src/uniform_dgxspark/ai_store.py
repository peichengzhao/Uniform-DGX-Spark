from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from .database import Database
from .schemas import (
    AI_PROVIDER_LABELS,
    AIAuditEventRead,
    AIDeploymentResultCreate,
    AIDeploymentResultRead,
    AIDeploymentReuseRead,
    AIDeploymentTemplateCreate,
    AIDeploymentTemplateRead,
    AISessionCreate,
    AISessionRead,
)
from .store import to_iso, utc_now


class AISessionNotFound(LookupError):
    pass


class AIDeploymentResultNotFound(LookupError):
    pass


class AIDeploymentTemplateNotFound(LookupError):
    pass


class AIDeploymentReusePending(RuntimeError):
    pass


class AIDeploymentReuseConflict(ValueError):
    pass


class AISessionStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        request: AISessionCreate,
        workspace_path: str,
        *,
        session_id: str | None = None,
        display_name: str | None = None,
        target_model_id: str | None = None,
    ) -> AISessionRead:
        session_id = session_id or str(uuid4())
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_sessions(
                    id, client_request_id, deployment_id, provider, task_type,
                    display_name, target_model_id,
                    ai_model, reasoning_effort, execution_mode, status, initial_prompt,
                    reference_urls_json, workspace_path, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'starting', ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    request.client_request_id,
                    request.deployment_id,
                    request.provider,
                    request.task_type,
                    display_name or request.display_name,
                    target_model_id or request.target_model_id,
                    request.ai_model,
                    request.reasoning_effort,
                    request.execution_mode,
                    request.initial_prompt,
                    json.dumps(request.reference_urls, separators=(",", ":")),
                    workspace_path,
                    now,
                    now,
                ),
            )
            self._audit(
                connection,
                session_id,
                "created",
                f"{request.provider} session created",
                {
                    "reference_count": len(request.reference_urls),
                    "execution_mode": request.execution_mode,
                    "ai_model": request.ai_model,
                    "reasoning_effort": request.reasoning_effort,
                },
                now,
            )
            connection.commit()
        return self.get(session_id)

    def get_by_client_request_id(self, client_request_id: str) -> AISessionRead:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM ai_sessions WHERE client_request_id = ?",
                (client_request_id,),
            ).fetchone()
        if row is None:
            raise AISessionNotFound(client_request_id)
        return self._from_row(row)

    def get(self, session_id: str) -> AISessionRead:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM ai_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise AISessionNotFound(session_id)
        return self._from_row(row)

    def list(
        self,
        *,
        limit: int = 100,
        include_archived: bool = False,
        target_model_id: str | None = None,
        task_type: str | None = None,
    ) -> tuple[list[AISessionRead], int]:
        filters: list[str] = []
        parameters: list[object] = []
        if not include_archived:
            filters.append("archived_at IS NULL")
        if target_model_id is not None:
            filters.append("target_model_id = ?")
            parameters.append(target_model_id)
        if task_type is not None:
            filters.append("task_type = ?")
            parameters.append(task_type)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        with self.database.connect() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM ai_sessions {where}", parameters
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"SELECT * FROM ai_sessions {where} ORDER BY created_at DESC LIMIT ?",
                (*parameters, limit),
            ).fetchall()
        return [self._from_row(row) for row in rows], total

    def archive(self, session_id: str) -> AISessionRead:
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE ai_sessions SET archived_at = ?, updated_at = ? WHERE id = ?",
                (now, now, session_id),
            )
            if cursor.rowcount == 0:
                raise AISessionNotFound(session_id)
            self._audit(connection, session_id, "archived", "AI session archived", {}, now)
            connection.commit()
        return self.get(session_id)

    def restore(self, session_id: str) -> AISessionRead:
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE ai_sessions SET archived_at = NULL, updated_at = ? WHERE id = ?",
                (now, session_id),
            )
            if cursor.rowcount == 0:
                raise AISessionNotFound(session_id)
            self._audit(connection, session_id, "restored", "AI session restored", {}, now)
            connection.commit()
        return self.get(session_id)

    def delete(self, session_id: str) -> None:
        with self.database.connect() as connection:
            cursor = connection.execute("DELETE FROM ai_sessions WHERE id = ?", (session_id,))
            if cursor.rowcount == 0:
                raise AISessionNotFound(session_id)
            connection.commit()

    def update_status(
        self,
        session_id: str,
        status: str,
        *,
        pid: int | None = None,
        exit_code: int | None = None,
        error_message: str | None = None,
        message: str | None = None,
    ) -> AISessionRead:
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE ai_sessions
                SET status = ?, pid = COALESCE(?, pid), exit_code = ?,
                    error_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, pid, exit_code, error_message, now, session_id),
            )
            if cursor.rowcount == 0:
                raise AISessionNotFound(session_id)
            self._audit(
                connection,
                session_id,
                "status_changed",
                message or f"Session status changed to {status}",
                {"status": status, "exit_code": exit_code},
                now,
            )
            connection.commit()
        return self.get(session_id)

    def link_deployment(self, session_id: str, deployment_id: str) -> AISessionRead:
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE ai_sessions SET deployment_id = ?, updated_at = ? WHERE id = ?",
                (deployment_id, now, session_id),
            )
            if cursor.rowcount == 0:
                raise AISessionNotFound(session_id)
            self._audit(
                connection,
                session_id,
                "deployment_linked",
                "AI session created a managed deployment",
                {"deployment_id": deployment_id},
                now,
            )
            connection.commit()
        return self.get(session_id)

    def add_audit(
        self,
        session_id: str,
        event_type: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM ai_sessions WHERE id = ?", (session_id,)
                ).fetchone()
                is None
            ):
                raise AISessionNotFound(session_id)
            self._audit(connection, session_id, event_type, message, details or {}, now)
            connection.commit()

    def list_audit(self, session_id: str) -> list[AIAuditEventRead]:
        self.get(session_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ai_audit_events WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
        return [
            AIAuditEventRead(
                id=int(row["id"]),
                session_id=row["session_id"],
                event_type=row["event_type"],
                message=row["message"],
                details=json.loads(row["details_json"]),
                created_at=datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")),
            )
            for row in rows
        ]

    def upsert_result(
        self,
        session_id: str,
        result: AIDeploymentResultCreate,
    ) -> AIDeploymentResultRead:
        self.get(session_id)
        now = to_iso(utc_now())
        payload = result.model_dump(mode="json")
        status = (
            "verified" if result.health_check.passed and result.inference_test.passed else "failed"
        )
        payload["status"] = status
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT created_at FROM ai_deployment_results WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            connection.execute(
                """
                INSERT INTO ai_deployment_results(
                    session_id, result_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    result_json = excluded.result_json,
                    updated_at = excluded.updated_at
                """,
                (
                    session_id,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    created_at,
                    now,
                ),
            )
            self._audit(
                connection,
                session_id,
                "deployment_result_reported",
                f"AI deployment result reported as {status}",
                {
                    "status": status,
                    "engine": result.engine,
                    "container_name": result.container_name,
                    "image": result.image,
                    "port": result.port,
                },
                now,
            )
            connection.commit()
        return self.get_result(session_id)

    def get_result(self, session_id: str) -> AIDeploymentResultRead:
        self.get(session_id)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM ai_deployment_results WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise AIDeploymentResultNotFound(session_id)
        payload = json.loads(row["result_json"])
        return AIDeploymentResultRead(
            session_id=session_id,
            created_at=datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")),
            updated_at=datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00")),
            **payload,
        )

    def list_results(
        self, *, limit: int = 200, include_archived: bool = False
    ) -> list[AIDeploymentResultRead]:
        archive_filter = "" if include_archived else "WHERE sessions.archived_at IS NULL"
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT results.* FROM ai_deployment_results AS results
                JOIN ai_sessions AS sessions ON sessions.id = results.session_id
                {archive_filter}
                ORDER BY results.updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        results: list[AIDeploymentResultRead] = []
        for row in rows:
            payload = json.loads(row["result_json"])
            results.append(
                AIDeploymentResultRead(
                    session_id=row["session_id"],
                    created_at=datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")),
                    updated_at=datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00")),
                    **payload,
                )
            )
        return results

    def upsert_template(self, template: AIDeploymentTemplateCreate) -> AIDeploymentTemplateRead:
        now = to_iso(utc_now())
        payload = template.model_dump(mode="json")
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT created_at FROM ai_deployment_templates WHERE id = ?",
                (template.id,),
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            connection.execute(
                """
                INSERT INTO ai_deployment_templates(
                    id, source_session_id, model_id, target_source, target_revision,
                    target_local_path, deployment_topology, template_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source_session_id = excluded.source_session_id,
                    model_id = excluded.model_id,
                    target_source = excluded.target_source,
                    target_revision = excluded.target_revision,
                    target_local_path = excluded.target_local_path,
                    deployment_topology = excluded.deployment_topology,
                    template_json = excluded.template_json,
                    updated_at = excluded.updated_at
                """,
                (
                    template.id,
                    template.source_session_id,
                    template.model_id,
                    template.target_source,
                    template.target_revision,
                    template.target_local_path,
                    template.deployment_topology,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    created_at,
                    now,
                ),
            )
            connection.commit()
        return self.get_template(template.id)

    def get_template(self, template_id: str) -> AIDeploymentTemplateRead:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM ai_deployment_templates WHERE id = ?", (template_id,)
            ).fetchone()
        if row is None:
            raise AIDeploymentTemplateNotFound(template_id)
        return self._template_from_row(row)

    def latest_template(
        self,
        *,
        model_id: str,
        target_source: str,
        target_revision: str | None = None,
        target_local_path: str | None = None,
        deployment_topology: str | None = None,
    ) -> AIDeploymentTemplateRead:
        filters = ["model_id = ?", "target_source = ?"]
        parameters: list[object] = [model_id, target_source]
        if target_revision is None:
            filters.append("target_revision IS NULL")
        else:
            filters.append("target_revision = ?")
            parameters.append(target_revision)
        if target_local_path is None:
            filters.append("target_local_path IS NULL")
        else:
            filters.append("target_local_path = ?")
            parameters.append(target_local_path)
        if deployment_topology is not None:
            filters.append("deployment_topology = ?")
            parameters.append(deployment_topology)
        with self.database.connect() as connection:
            row = connection.execute(
                f"""
                SELECT * FROM ai_deployment_templates
                WHERE {" AND ".join(filters)}
                ORDER BY updated_at DESC LIMIT 1
                """,
                parameters,
            ).fetchone()
        if row is None:
            raise AIDeploymentTemplateNotFound(model_id)
        return self._template_from_row(row)

    def begin_reuse_request(
        self, client_request_id: str, request_fingerprint: str
    ) -> AIDeploymentReuseRead | None:
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO ai_deployment_reuse_requests(
                    client_request_id, request_fingerprint, response_json,
                    created_at, updated_at
                ) VALUES (?, ?, NULL, ?, ?)
                """,
                (client_request_id, request_fingerprint, now, now),
            )
            if cursor.rowcount == 1:
                connection.commit()
                return None
            row = connection.execute(
                "SELECT * FROM ai_deployment_reuse_requests WHERE client_request_id = ?",
                (client_request_id,),
            ).fetchone()
        if row is None:
            raise AIDeploymentReusePending(client_request_id)
        if row["request_fingerprint"] != request_fingerprint:
            raise AIDeploymentReuseConflict(
                "client_request_id was already used for a different deployment request"
            )
        if row["response_json"] is None:
            stale_before = to_iso(utc_now() - timedelta(minutes=5))
            with self.database.connect() as connection:
                reclaimed = connection.execute(
                    """
                    UPDATE ai_deployment_reuse_requests
                    SET updated_at = ?
                    WHERE client_request_id = ? AND request_fingerprint = ?
                        AND response_json IS NULL AND updated_at < ?
                    """,
                    (
                        now,
                        client_request_id,
                        request_fingerprint,
                        stale_before,
                    ),
                )
                connection.commit()
            if reclaimed.rowcount == 1:
                return None
            raise AIDeploymentReusePending(client_request_id)
        return AIDeploymentReuseRead.model_validate_json(row["response_json"])

    def complete_reuse_request(
        self, client_request_id: str, response: AIDeploymentReuseRead
    ) -> None:
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE ai_deployment_reuse_requests
                SET response_json = ?, updated_at = ?
                WHERE client_request_id = ?
                """,
                (response.model_dump_json(), now, client_request_id),
            )
            if cursor.rowcount == 0:
                raise AIDeploymentReusePending(client_request_id)
            connection.commit()

    def abandon_reuse_request(self, client_request_id: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                DELETE FROM ai_deployment_reuse_requests
                WHERE client_request_id = ? AND response_json IS NULL
                """,
                (client_request_id,),
            )
            connection.commit()

    @staticmethod
    def _template_from_row(row: sqlite3.Row) -> AIDeploymentTemplateRead:
        payload = json.loads(row["template_json"])
        payload["source_session_id"] = row["source_session_id"]
        return AIDeploymentTemplateRead(
            **payload,
            created_at=datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")),
            updated_at=datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00")),
        )

    def recover_incomplete(self) -> int:
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM ai_sessions WHERE status IN ('starting', 'running')"
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE ai_sessions SET status = 'interrupted', pid = NULL,
                        error_message = 'Control service restarted', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, row["id"]),
                )
                self._audit(
                    connection,
                    row["id"],
                    "recovered",
                    "Session marked interrupted after service restart",
                    {},
                    now,
                )
            connection.commit()
        return len(rows)

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        session_id: str,
        event_type: str,
        message: str,
        details: dict[str, object],
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO ai_audit_events(
                session_id, event_type, message, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                session_id,
                event_type,
                message,
                json.dumps(details, separators=(",", ":"), sort_keys=True),
                created_at,
            ),
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> AISessionRead:
        target_model_id = row["target_model_id"]
        display_name = row["display_name"]
        related_session_id: str | None = None
        related_service_name: str | None = None
        try:
            context = json.loads(
                (Path(row["workspace_path"]) / "deployment-context.json").read_text(
                    encoding="utf-8"
                )
            )
            deployment = context.get("deployment") or {}
            if isinstance(deployment, dict):
                service = deployment.get("service")
                if isinstance(service, dict):
                    related_service_name = service.get("label") or service.get("model_name")
                    if service.get("target_type") == "ai_session":
                        related_session_id = service.get("target_id")
                handoff = deployment.get("service_handoff")
                if isinstance(handoff, dict):
                    source = handoff.get("source_session")
                    if isinstance(source, dict):
                        related_session_id = source.get("id") or related_session_id
                        related_service_name = (
                            source.get("display_name")
                            or source.get("target_model_id")
                            or related_service_name
                        )
                        target_model_id = target_model_id or source.get("target_model_id")
            if not target_model_id:
                target = deployment.get("target_model") or {}
                target_model_id = target.get("model_id") or deployment.get("model_id")
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        if not display_name:
            display_name = (
                str(target_model_id).rsplit("/", 1)[-1]
                if target_model_id
                else f"{AI_PROVIDER_LABELS.get(row['provider'], row['provider'])} 会话"
            )
        return AISessionRead(
            id=row["id"],
            deployment_id=row["deployment_id"],
            provider=row["provider"],
            task_type=row["task_type"],
            display_name=display_name,
            target_model_id=target_model_id,
            related_session_id=related_session_id,
            related_service_name=related_service_name,
            ai_model=row["ai_model"],
            reasoning_effort=row["reasoning_effort"],
            execution_mode=row["execution_mode"],
            status=row["status"],
            initial_prompt=row["initial_prompt"],
            reference_urls=json.loads(row["reference_urls_json"]),
            workspace_path=row["workspace_path"],
            pid=row["pid"],
            exit_code=row["exit_code"],
            error_message=row["error_message"],
            archived_at=(
                datetime.fromisoformat(row["archived_at"].replace("Z", "+00:00"))
                if row["archived_at"]
                else None
            ),
            created_at=datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")),
            updated_at=datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00")),
        )
