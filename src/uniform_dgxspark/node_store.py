from __future__ import annotations

import sqlite3
from datetime import datetime
from uuid import uuid4

from .database import Database
from .schemas import SSHNodeCreate, SSHNodeRead
from .store import to_iso, utc_now


class SSHNodeNotFound(LookupError):
    pass


class SSHNodeAlreadyExists(ValueError):
    pass


class SSHNodeStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, request: SSHNodeCreate) -> SSHNodeRead:
        node_id = str(uuid4())
        now = to_iso(utc_now())
        try:
            with self.database.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO ssh_nodes(id, label, host, port, username, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        node_id,
                        request.label,
                        request.host,
                        request.port,
                        request.username,
                        now,
                        now,
                    ),
                )
                connection.commit()
        except sqlite3.IntegrityError as error:
            raise SSHNodeAlreadyExists("SSH node is already registered") from error
        return self.get(node_id)

    def get(self, node_id: str) -> SSHNodeRead:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM ssh_nodes WHERE id = ?", (node_id,)).fetchone()
        if row is None:
            raise SSHNodeNotFound(node_id)
        return self._from_row(row)

    def list(self) -> list[SSHNodeRead]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT * FROM ssh_nodes ORDER BY created_at, id").fetchall()
        return [self._from_row(row) for row in rows]

    def delete(self, node_id: str) -> bool:
        with self.database.connect() as connection:
            cursor = connection.execute("DELETE FROM ssh_nodes WHERE id = ?", (node_id,))
            connection.commit()
        return cursor.rowcount > 0

    @staticmethod
    def _from_row(row: sqlite3.Row) -> SSHNodeRead:
        return SSHNodeRead(
            id=row["id"],
            label=row["label"],
            host=row["host"],
            port=int(row["port"]),
            username=row["username"],
            created_at=datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")),
            updated_at=datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00")),
        )
