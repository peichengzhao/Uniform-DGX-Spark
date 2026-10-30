from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from .database import Database
from .store import to_iso, utc_now


@dataclass(frozen=True, slots=True)
class ServicePublication:
    publication_id: str
    target_type: str
    target_id: str
    api_key_hash: str
    key_hint: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class ServicePublicationStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get_for_target(self, target_type: str, target_id: str) -> ServicePublication | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM service_publications
                WHERE target_type = ? AND target_id = ?
                """,
                (target_type, target_id),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def get_enabled_by_id(self, publication_id: str) -> ServicePublication | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM service_publications
                WHERE publication_id = ? AND enabled = 1
                """,
                (publication_id,),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def enable(
        self,
        target_type: str,
        target_id: str,
        *,
        api_key_hash: str,
        key_hint: str,
    ) -> ServicePublication:
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT publication_id, created_at FROM service_publications
                WHERE target_type = ? AND target_id = ?
                """,
                (target_type, target_id),
            ).fetchone()
            publication_id = (
                str(existing["publication_id"]) if existing is not None else uuid4().hex
            )
            created_at = str(existing["created_at"]) if existing is not None else now
            connection.execute(
                """
                INSERT INTO service_publications(
                    publication_id, target_type, target_id, api_key_hash, key_hint,
                    enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(target_type, target_id) DO UPDATE SET
                    api_key_hash = excluded.api_key_hash,
                    key_hint = excluded.key_hint,
                    enabled = 1,
                    updated_at = excluded.updated_at
                """,
                (
                    publication_id,
                    target_type,
                    target_id,
                    api_key_hash,
                    key_hint,
                    created_at,
                    now,
                ),
            )
            connection.commit()
        publication = self.get_for_target(target_type, target_id)
        if publication is None:  # pragma: no cover - protected by the transaction above
            raise sqlite3.DatabaseError("service publication was not persisted")
        return publication

    def disable(self, target_type: str, target_id: str) -> bool:
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE service_publications
                SET api_key_hash = '', key_hint = '', enabled = 0, updated_at = ?
                WHERE target_type = ? AND target_id = ? AND enabled = 1
                """,
                (now, target_type, target_id),
            )
            connection.commit()
        return cursor.rowcount > 0

    def count_enabled(self) -> int:
        with self.database.connect() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM service_publications WHERE enabled = 1"
                ).fetchone()[0]
            )

    def enabled_publication_ids(self) -> set[str]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT publication_id FROM service_publications
                WHERE enabled = 1
                """
            ).fetchall()
        return {str(row["publication_id"]) for row in rows}

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ServicePublication:
        return ServicePublication(
            publication_id=str(row["publication_id"]),
            target_type=str(row["target_type"]),
            target_id=str(row["target_id"]),
            api_key_hash=str(row["api_key_hash"]),
            key_hint=str(row["key_hint"]),
            enabled=bool(row["enabled"]),
            created_at=datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00")),
            updated_at=datetime.fromisoformat(str(row["updated_at"]).replace("Z", "+00:00")),
        )
