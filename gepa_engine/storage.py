"""Durable JSON records keyed by (kind, id) in one SQLite file; provider secrets never live here.

Several processes may share the file (a detached job runner writes while the CLI or the MCP
server reads), so writes that depend on the current value go through :meth:`Storage.update`,
which holds an immediate SQLite transaction.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class Storage:
    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "gepa.sqlite3"
        self.lock = threading.RLock()
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, body TEXT NOT NULL, PRIMARY KEY(kind,id))")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def put(self, kind: str, identifier: str, value: Any) -> Any:
        with self.lock, self.connect() as db:
            db.execute("INSERT OR REPLACE INTO records VALUES (?, ?, ?)", (kind, identifier, json.dumps(value, ensure_ascii=False)))
        return value

    def insert(self, kind: str, identifier: str, value: Any) -> bool:
        """Store ``value`` only if the key is new; an existing record is never replaced."""
        with self.lock, self.connect() as db:
            cursor = db.execute("INSERT OR IGNORE INTO records VALUES (?, ?, ?)", (kind, identifier, json.dumps(value, ensure_ascii=False)))
        return cursor.rowcount == 1

    def get(self, kind: str, identifier: str) -> Any:
        with self.lock, self.connect() as db:
            row = db.execute("SELECT body FROM records WHERE kind=? AND id=?", (kind, identifier)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, kind: str) -> list[Any]:
        with self.lock, self.connect() as db:
            rows = db.execute("SELECT body FROM records WHERE kind=? ORDER BY rowid DESC", (kind,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def update(self, kind: str, identifier: str, change: Callable[[Any], Any]) -> Any:
        """Read, change and write one record atomically across processes; ``KeyError`` if it does not exist.

        ``change`` may raise to abort without writing.
        """
        with self.lock:
            db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
            try:
                db.execute("BEGIN IMMEDIATE")
                try:
                    row = db.execute("SELECT body FROM records WHERE kind=? AND id=?", (kind, identifier)).fetchone()
                    if row is None:
                        raise KeyError(identifier)
                    value = change(json.loads(row[0]))
                    db.execute("INSERT OR REPLACE INTO records VALUES (?, ?, ?)", (kind, identifier, json.dumps(value, ensure_ascii=False)))
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
                db.execute("COMMIT")
                return value
            finally:
                db.close()
