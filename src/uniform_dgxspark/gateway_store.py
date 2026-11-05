from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from secrets import token_urlsafe
from uuid import uuid4

from .database import Database
from .store import to_iso, utc_now


class GatewayConsumerNotFound(LookupError):
    pass


class GatewayTokenNotFound(LookupError):
    pass


class GatewayPublicationScopeInvalid(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GatewayConsumer:
    consumer_id: str
    name: str
    note: str | None
    enabled: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class GatewayToken:
    token_id: str
    consumer_id: str
    consumer_name: str
    consumer_enabled: bool
    name: str
    key_hint: str
    enabled: bool
    token_limit: int | None
    used_tokens: int
    reserved_tokens: int
    expires_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime
    publication_ids: tuple[str, ...]
    model_ids: tuple[str, ...]

    def expires_in_past(self, now: datetime | None = None) -> bool:
        return self.expires_at is not None and self.expires_at <= (now or utc_now())

    def quota_exhausted(self) -> bool:
        return self.token_limit is not None and self.used_tokens >= self.token_limit

    def is_usable(self, now: datetime | None = None) -> bool:
        return (
            self.enabled
            and self.consumer_enabled
            and not self.expires_in_past(now)
            and not self.quota_exhausted()
        )


class GatewayStore:
    KEY_PREFIX = "udgs_"

    def __init__(self, database: Database) -> None:
        self.database = database

    def create_consumer(self, name: str, note: str | None = None) -> GatewayConsumer:
        consumer_id = uuid4().hex
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO gateway_consumers(
                    consumer_id, name, note, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, 1, ?, ?)
                """,
                (consumer_id, name.strip(), note.strip() if note else None, now, now),
            )
            connection.commit()
        return self.get_consumer(consumer_id)

    def get_consumer(self, consumer_id: str) -> GatewayConsumer:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM gateway_consumers WHERE consumer_id = ?", (consumer_id,)
            ).fetchone()
        if row is None:
            raise GatewayConsumerNotFound(consumer_id)
        return self._consumer_from_row(row)

    def list_consumers(self) -> list[tuple[GatewayConsumer, list[GatewayToken]]]:
        with self.database.connect() as connection:
            consumer_rows = connection.execute(
                "SELECT * FROM gateway_consumers ORDER BY created_at DESC, consumer_id"
            ).fetchall()
            token_rows = connection.execute(
                """
                SELECT token.*, consumer.name AS consumer_name,
                       consumer.enabled AS consumer_enabled
                FROM gateway_tokens AS token
                JOIN gateway_consumers AS consumer
                  ON consumer.consumer_id = token.consumer_id
                ORDER BY token.created_at DESC, token.token_id
                """
            ).fetchall()
            scope_rows = connection.execute(
                "SELECT token_id, publication_id FROM gateway_token_publications"
            ).fetchall()
            model_scope_rows = connection.execute(
                "SELECT token_id, model_id FROM gateway_token_models"
            ).fetchall()
            active_publication_rows = connection.execute(
                """
                SELECT publication_id, model_key FROM service_publications
                WHERE enabled = 1
                """
            ).fetchall()
        legacy_scopes: dict[str, list[str]] = {}
        for row in scope_rows:
            legacy_scopes.setdefault(str(row["token_id"]), []).append(
                str(row["publication_id"])
            )
        model_scopes: dict[str, list[str]] = {}
        for row in model_scope_rows:
            model_scopes.setdefault(str(row["token_id"]), []).append(str(row["model_id"]))
        active_publications: dict[str, list[str]] = {}
        for row in active_publication_rows:
            active_publications.setdefault(str(row["model_key"]), []).append(
                str(row["publication_id"])
            )
        tokens_by_consumer: dict[str, list[GatewayToken]] = {}
        for row in token_rows:
            token_id = str(row["token_id"])
            model_ids = model_scopes.get(token_id, [])
            publication_ids = (
                sorted(
                    {
                        publication_id
                        for model_id in model_ids
                        for publication_id in active_publications.get(model_id, [])
                    }
                )
                if model_ids
                else legacy_scopes.get(token_id, [])
            )
            token = self._token_from_row(row, publication_ids, model_ids)
            tokens_by_consumer.setdefault(token.consumer_id, []).append(token)
        return [
            (self._consumer_from_row(row), tokens_by_consumer.get(str(row["consumer_id"]), []))
            for row in consumer_rows
        ]

    def disable_consumer(self, consumer_id: str) -> bool:
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE gateway_consumers
                SET enabled = 0, updated_at = ?
                WHERE consumer_id = ? AND enabled = 1
                """,
                (now, consumer_id),
            )
            connection.execute(
                "UPDATE gateway_tokens SET enabled = 0, updated_at = ? WHERE consumer_id = ?",
                (now, consumer_id),
            )
            connection.commit()
        return cursor.rowcount > 0

    def delete_consumer(self, consumer_id: str) -> bool:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM gateway_consumers WHERE consumer_id = ?", (consumer_id,)
            )
            connection.commit()
        return cursor.rowcount > 0

    def create_token(
        self,
        consumer_id: str,
        name: str,
        publication_ids: list[str],
        *,
        token_limit: int | None = None,
        expires_at: datetime | None = None,
    ) -> tuple[GatewayToken, str]:
        consumer = self.get_consumer(consumer_id)
        if not consumer.enabled:
            raise GatewayConsumerNotFound(consumer_id)
        selected = list(dict.fromkeys(publication_ids))
        if not selected:
            raise GatewayPublicationScopeInvalid("select at least one published model")
        placeholders = ",".join("?" for _ in selected)
        with self.database.connect() as connection:
            available_rows = connection.execute(
                f"""
                    SELECT publication_id, model_key FROM service_publications
                    WHERE enabled = 1 AND publication_id IN ({placeholders})
                    """,
                tuple(selected),
            ).fetchall()
            available = {
                str(row["publication_id"]): str(row["model_key"])
                for row in available_rows
            }
            if set(available) != set(selected):
                raise GatewayPublicationScopeInvalid(
                    "one or more selected models are not published"
                )
            model_ids = sorted(
                {model_id for model_id in available.values() if model_id}
            )
            if not model_ids:
                raise GatewayPublicationScopeInvalid(
                    "selected models do not have a reusable model identity"
                )
            api_key = f"{self.KEY_PREFIX}{token_urlsafe(32)}"
            token_id = uuid4().hex
            now = to_iso(utc_now())
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO gateway_tokens(
                    token_id, consumer_id, name, key_hash, key_hint, enabled,
                    token_limit, used_tokens, reserved_tokens, expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, 0, 0, ?, ?, ?)
                """,
                (
                    token_id,
                    consumer_id,
                    name.strip(),
                    self.hash_api_key(api_key),
                    self.key_hint(api_key),
                    token_limit,
                    to_iso(expires_at) if expires_at else None,
                    now,
                    now,
                ),
            )
            connection.executemany(
                """
                INSERT INTO gateway_token_publications(token_id, publication_id, created_at)
                VALUES (?, ?, ?)
                """,
                [(token_id, publication_id, now) for publication_id in selected],
            )
            connection.executemany(
                """
                INSERT INTO gateway_token_models(token_id, model_id, created_at)
                VALUES (?, ?, ?)
                """,
                [(token_id, model_id, now) for model_id in model_ids],
            )
            connection.commit()
        return self.get_token(token_id), api_key

    def get_token(self, token_id: str) -> GatewayToken:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT token.*, consumer.name AS consumer_name,
                       consumer.enabled AS consumer_enabled
                FROM gateway_tokens AS token
                JOIN gateway_consumers AS consumer
                  ON consumer.consumer_id = token.consumer_id
                WHERE token.token_id = ?
                """,
                (token_id,),
            ).fetchone()
            publication_ids, model_ids = (
                self._scopes(connection, token_id) if row else ([], [])
            )
        if row is None:
            raise GatewayTokenNotFound(token_id)
        return self._token_from_row(row, publication_ids, model_ids)

    def reserve_tokens(
        self, token_id: str, requested: int, *, strict: bool = False
    ) -> int | None:
        """Atomically reserve part of a token's remaining quota.

        Returns the amount reserved. A token without a total limit reserves
        zero; an exhausted token returns None. Reserving before proxying closes
        the race where many concurrent requests all pass a stale quota check.
        """
        requested = max(int(requested), 0)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT token_limit, used_tokens, reserved_tokens, enabled
                FROM gateway_tokens WHERE token_id = ?
                """,
                (token_id,),
            ).fetchone()
            if row is None or not bool(row["enabled"]):
                connection.rollback()
                return None
            token_limit = (
                int(row["token_limit"]) if row["token_limit"] is not None else None
            )
            if token_limit is None:
                connection.commit()
                return 0
            remaining = token_limit - int(row["used_tokens"]) - int(
                row["reserved_tokens"]
            )
            if remaining <= 0:
                connection.rollback()
                return None
            if strict and requested > remaining:
                connection.rollback()
                return None
            amount = min(max(requested, 1), remaining)
            connection.execute(
                """
                UPDATE gateway_tokens
                SET reserved_tokens = reserved_tokens + ?, updated_at = ?
                WHERE token_id = ?
                """,
                (amount, to_iso(utc_now()), token_id),
            )
            connection.commit()
            return amount

    def release_reserved_tokens(self, token_id: str, amount: int) -> None:
        if amount <= 0:
            return
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE gateway_tokens
                SET reserved_tokens = CASE
                    WHEN reserved_tokens >= ? THEN reserved_tokens - ?
                    ELSE 0
                END,
                updated_at = ?
                WHERE token_id = ?
                """,
                (amount, amount, to_iso(utc_now()), token_id),
            )
            connection.commit()

    def find_token_by_key(self, api_key: str) -> GatewayToken | None:
        if not api_key or len(api_key) > 512:
            return None
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT token.*, consumer.name AS consumer_name,
                       consumer.enabled AS consumer_enabled
                FROM gateway_tokens AS token
                JOIN gateway_consumers AS consumer
                  ON consumer.consumer_id = token.consumer_id
                WHERE token.key_hash = ?
                """,
                (self.hash_api_key(api_key),),
            ).fetchone()
            publication_ids, model_ids = (
                self._scopes(connection, str(row["token_id"])) if row else ([], [])
            )
        return (
            self._token_from_row(row, publication_ids, model_ids)
            if row is not None
            else None
        )

    def can_access_publication(
        self,
        token_id: str,
        publication_id: str,
        model_id: str = "",
    ) -> bool:
        with self.database.connect() as connection:
            model_scope = connection.execute(
                "SELECT 1 FROM gateway_token_models WHERE token_id = ? LIMIT 1",
                (token_id,),
            ).fetchone()
            if model_scope is not None:
                allowed = connection.execute(
                    """
                    SELECT 1 FROM gateway_token_models
                    WHERE token_id = ? AND model_id = ?
                    """,
                    (token_id, model_id.strip()),
                ).fetchone()
                return allowed is not None
            legacy_scope = connection.execute(
                """
                SELECT 1 FROM gateway_token_publications
                WHERE token_id = ? AND publication_id = ?
                """,
                (token_id, publication_id),
            ).fetchone()
        return legacy_scope is not None

    def disable_token(self, token_id: str) -> bool:
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE gateway_tokens
                SET enabled = 0, updated_at = ?
                WHERE token_id = ? AND enabled = 1
                """,
                (now, token_id),
            )
            connection.commit()
        return cursor.rowcount > 0

    def record_usage(
        self,
        *,
        token_id: str | None,
        consumer_id: str | None,
        publication_id: str,
        publication_label: str,
        target_type: str,
        target_id: str,
        model: str | None,
        endpoint: str,
        method: str,
        status_code: int,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
        usage_source: str,
        latency_ms: int,
        request_id: str | None,
        reserved_tokens: int = 0,
        charge_reserved: bool = False,
    ) -> None:
        now = to_iso(utc_now())
        reservation = max(int(reserved_tokens), 0)
        used_tokens = max(total_tokens or 0, 0)
        if charge_reserved and total_tokens is None and reservation:
            used_tokens = reservation
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO gateway_usage_logs(
                    id, token_id, consumer_id, publication_id, publication_label,
                    target_type, target_id, model, endpoint, method, status_code,
                    prompt_tokens, completion_tokens, total_tokens, usage_source,
                    latency_ms, request_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid4().hex,
                    token_id,
                    consumer_id,
                    publication_id,
                    publication_label,
                    target_type,
                    target_id,
                    model,
                    endpoint,
                    method,
                    status_code,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    usage_source,
                    max(latency_ms, 0),
                    request_id,
                    now,
                ),
            )
            if token_id:
                connection.execute(
                    """
                    UPDATE gateway_tokens
                    SET used_tokens = used_tokens + ?,
                        reserved_tokens = CASE
                            WHEN reserved_tokens >= ? THEN reserved_tokens - ?
                            ELSE 0
                        END,
                        last_used_at = ?
                    WHERE token_id = ?
                    """,
                    (used_tokens, reservation, reservation, now, token_id),
                )
            connection.commit()

    def usage_page(
        self,
        *,
        publication_id: str | None = None,
        consumer_id: str | None = None,
        token_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, object]], int, dict[str, int]]:
        clauses: list[str] = []
        parameters: list[object] = []
        for column, value in (
            ("usage.publication_id", publication_id),
            ("usage.consumer_id", consumer_id),
            ("usage.token_id", token_id),
        ):
            if value:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.database.connect() as connection:
            summary_row = connection.execute(
                f"""
                SELECT COUNT(*) AS requests,
                       COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       COALESCE(SUM(CASE WHEN usage_source = 'unavailable' THEN 1 ELSE 0 END), 0)
                           AS unknown_usage
                FROM gateway_usage_logs AS usage
                {where}
                """,
                tuple(parameters),
            ).fetchone()
            rows = connection.execute(
                f"""
                SELECT usage.*, consumer.name AS consumer_name, token.name AS token_name
                FROM gateway_usage_logs AS usage
                LEFT JOIN gateway_consumers AS consumer
                  ON consumer.consumer_id = usage.consumer_id
                LEFT JOIN gateway_tokens AS token ON token.token_id = usage.token_id
                {where}
                ORDER BY usage.created_at DESC, usage.id DESC
                LIMIT ? OFFSET ?
                """,
                (*parameters, limit, offset),
            ).fetchall()
        items = [
            {
                **dict(row),
                "consumer_name": row["consumer_name"] or "未分配调用方",
                "token_name": row["token_name"] or "历史共享 Key",
            }
            for row in rows
        ]
        summary = {
            "requests": int(summary_row["requests"]),
            "prompt_tokens": int(summary_row["prompt_tokens"]),
            "completion_tokens": int(summary_row["completion_tokens"]),
            "total_tokens": int(summary_row["total_tokens"]),
            "unknown_usage": int(summary_row["unknown_usage"]),
        }
        return items, int(summary_row["requests"]), summary

    @classmethod
    def hash_api_key(cls, api_key: str) -> str:
        return hashlib.sha256(api_key.encode("utf-8")).hexdigest()

    @classmethod
    def key_hint(cls, api_key: str) -> str:
        return f"{api_key[:10]}…{api_key[-4:]}"

    @staticmethod
    def _scope_ids(connection: sqlite3.Connection, token_id: str) -> list[str]:
        return [
            str(row["publication_id"])
            for row in connection.execute(
                """
                SELECT publication_id FROM gateway_token_publications
                WHERE token_id = ? ORDER BY publication_id
                """,
                (token_id,),
            ).fetchall()
        ]

    @classmethod
    def _scopes(
        cls, connection: sqlite3.Connection, token_id: str
    ) -> tuple[list[str], list[str]]:
        model_ids = [
            str(row["model_id"])
            for row in connection.execute(
                """
                SELECT model_id FROM gateway_token_models
                WHERE token_id = ? ORDER BY model_id
                """,
                (token_id,),
            ).fetchall()
        ]
        if not model_ids:
            return cls._scope_ids(connection, token_id), []
        placeholders = ",".join("?" for _ in model_ids)
        publication_ids = [
            str(row["publication_id"])
            for row in connection.execute(
                f"""
                SELECT publication_id FROM service_publications
                WHERE enabled = 1 AND model_key IN ({placeholders})
                ORDER BY publication_id
                """,
                tuple(model_ids),
            ).fetchall()
        ]
        return publication_ids, model_ids

    @staticmethod
    def _consumer_from_row(row: sqlite3.Row) -> GatewayConsumer:
        return GatewayConsumer(
            consumer_id=str(row["consumer_id"]),
            name=str(row["name"]),
            note=str(row["note"]) if row["note"] is not None else None,
            enabled=bool(row["enabled"]),
            created_at=datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00")),
            updated_at=datetime.fromisoformat(str(row["updated_at"]).replace("Z", "+00:00")),
        )

    @staticmethod
    def _token_from_row(
        row: sqlite3.Row,
        publication_ids: list[str],
        model_ids: list[str],
    ) -> GatewayToken:
        return GatewayToken(
            token_id=str(row["token_id"]),
            consumer_id=str(row["consumer_id"]),
            consumer_name=str(row["consumer_name"]),
            consumer_enabled=bool(row["consumer_enabled"]),
            name=str(row["name"]),
            key_hint=str(row["key_hint"]),
            enabled=bool(row["enabled"]),
            token_limit=int(row["token_limit"]) if row["token_limit"] is not None else None,
            used_tokens=int(row["used_tokens"]),
            reserved_tokens=int(row["reserved_tokens"]),
            expires_at=(
                datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
                if row["expires_at"] is not None
                else None
            ),
            last_used_at=(
                datetime.fromisoformat(str(row["last_used_at"]).replace("Z", "+00:00"))
                if row["last_used_at"] is not None
                else None
            ),
            created_at=datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00")),
            updated_at=datetime.fromisoformat(str(row["updated_at"]).replace("Z", "+00:00")),
            publication_ids=tuple(publication_ids),
            model_ids=tuple(model_ids),
        )
