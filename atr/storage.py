"""Transactional metadata and immutable, content-addressed managed objects."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from atr.errors import ATRError
from atr.models import Artifact, Event, canonical


def digest(value: bytes | str) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


class Redactor:
    def __init__(self, secrets: list[str] | None = None) -> None:
        self.secrets = [s for s in (secrets or []) if s]

    def text(self, value: str) -> str:
        for secret in sorted(self.secrets, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9_./+=-]+", r"\1[REDACTED]", value)
        return re.sub(r'''(?i)((?:api[_-]?key|password|secret|token)["']?\s*[:=]\s*["']?)[^\s,"'\}\]]+''',
                      r"\1[REDACTED]", value)

    def data(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {str(k): ("[REDACTED]" if re.search(
                r"(?i)(password|api[_-]?key|secret|authorization|access_token)", str(k))
                else self.data(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [self.data(x) for x in value]
        return value


class Store:
    def __init__(self, home: Path, redactor: Redactor) -> None:
        self.home = home.resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.objects = self.home / "objects"
        self.objects.mkdir(exist_ok=True)
        self.redactor = redactor
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.home / "registry.sqlite3", timeout=30,
                                  check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=30000")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            self.db.close()
            raise ATRError("schema_version", "unsupported metadata schema version")
        if version == 1:
            self.db.execute("ALTER TABLE runs ADD COLUMN dynamic INTEGER NOT NULL DEFAULT 0")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS tools (
          id TEXT NOT NULL, version TEXT NOT NULL, program_ref TEXT NOT NULL,
          manifest TEXT NOT NULL, state TEXT NOT NULL, reviewed INTEGER NOT NULL DEFAULT 0,
          review TEXT, created REAL NOT NULL, window_start REAL NOT NULL, archived REAL,
          last_used REAL, epoch INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(id,version));
        CREATE TABLE IF NOT EXISTS active (id TEXT PRIMARY KEY, version TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS candidates (
          id TEXT PRIMARY KEY, program_ref TEXT NOT NULL, signature TEXT NOT NULL,
          manifest TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
          root_task_id TEXT NOT NULL, run_id TEXT NOT NULL, state TEXT NOT NULL,
          UNIQUE(signature));
        CREATE TABLE IF NOT EXISTS dependencies (
          parent_id TEXT NOT NULL, parent_version TEXT NOT NULL,
          child_id TEXT NOT NULL, child_version TEXT NOT NULL,
          PRIMARY KEY(parent_id,parent_version,child_id,child_version));
        CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
          entity UNINDEXED, id UNINDEXED, version UNINDEXED, body,
          tokenize='unicode61');
        CREATE TABLE IF NOT EXISTS runs (
          id TEXT PRIMARY KEY, task TEXT NOT NULL, operation TEXT, payload TEXT NOT NULL,
          tool_id TEXT, version TEXT, epoch INTEGER NOT NULL DEFAULT 0,
          parent TEXT, input_key TEXT NOT NULL, retry_of TEXT, cache_hit INTEGER NOT NULL,
          review_run INTEGER NOT NULL DEFAULT 0, started REAL NOT NULL, finished REAL,
          result TEXT NOT NULL, eligible INTEGER NOT NULL DEFAULT 0,
          cancel_requested INTEGER NOT NULL DEFAULT 0,
          dynamic INTEGER NOT NULL DEFAULT 0,
          UNIQUE(task, operation));
        CREATE INDEX IF NOT EXISTS runs_tool ON runs(tool_id, version, epoch);
        CREATE TABLE IF NOT EXISTS artifacts (
          ref TEXT NOT NULL, run_id TEXT NOT NULL, name TEXT NOT NULL, mime TEXT NOT NULL,
          size INTEGER NOT NULL, created REAL NOT NULL, PRIMARY KEY(ref,run_id,name));
        CREATE TABLE IF NOT EXISTS events (
          id TEXT PRIMARY KEY, kind TEXT NOT NULL, task TEXT, run_id TEXT,
          timestamp REAL NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS costs (
          id TEXT PRIMARY KEY, task TEXT NOT NULL, run_id TEXT, timestamp REAL NOT NULL,
          category TEXT NOT NULL, currency TEXT NOT NULL, amount REAL,
          data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS switches (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, started REAL NOT NULL);
        INSERT OR IGNORE INTO switches VALUES ('auto_create',1), ('dynamic_execute',1);
        CREATE TABLE IF NOT EXISTS task_budget (
          task TEXT PRIMARY KEY, reserved REAL NOT NULL DEFAULT 0,
          creations INTEGER NOT NULL DEFAULT 0, repairs INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS reservations (
          id TEXT PRIMARY KEY, task TEXT NOT NULL, maximum REAL NOT NULL,
          run_id TEXT, state TEXT NOT NULL DEFAULT 'pending');
        CREATE TABLE IF NOT EXISTS project_locks (project TEXT PRIMARY KEY, run_id TEXT NOT NULL);
        PRAGMA user_version=2;
        """)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, params)]

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = self.rows(sql, params)
        return rows[0] if rows else None

    def put(self, content: bytes) -> str:
        key = digest(content)
        path = self.objects / key[:2] / key[2:]
        path.parent.mkdir(exist_ok=True)
        # Publish complete bytes atomically; readers never see a half-written object.
        if not path.is_file():
            descriptor, temporary = tempfile.mkstemp(prefix=".stage-", dir=path.parent)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.isfile(temporary):
                    os.unlink(temporary)
        return "artifact:sha256/" + key

    def object_path(self, ref: str) -> Path:
        if not re.fullmatch(r"artifact:sha256/[a-f0-9]{64}", ref):
            raise ATRError("invalid_ref", "invalid managed object reference")
        key = ref.rsplit("/", 1)[-1]
        path = self.objects / key[:2] / key[2:]
        if path.is_symlink() or path.parent.is_symlink():
            raise ATRError("invalid_ref", "managed object cannot be a symbolic link")
        return path

    def get(self, ref: str) -> bytes:
        try:
            content = self.object_path(ref).read_bytes()
            if digest(content) != ref.rsplit("/", 1)[-1]:
                raise ATRError("object_corrupt", "managed object integrity failed")
            return content
        except FileNotFoundError as exc:
            raise ATRError("missing_artifact", "managed object no longer available") from exc

    def artifact(self, run_id: str, name: str, content: bytes, now: datetime,
                 mime: str = "text/plain") -> Artifact:
        # The public artifact surface is text/JSON; binary output must be explicitly encoded.
        content = self.redactor.text(content.decode("utf-8", errors="replace")).encode()
        with self.transaction() as db:
            ref = self.put(content)
            db.execute("INSERT OR IGNORE INTO artifacts VALUES (?,?,?,?,?,?)",
                       (ref, run_id, name, mime, len(content), now.timestamp()))
        return Artifact(ref=ref, type=mime, size_bytes=len(content), name=name)

    def read_artifact(self, ref: str, offset: int = 0, limit: int = 4096) -> dict[str, Any]:
        if not 0 <= offset or not 1 <= limit <= 16000:
            raise ATRError("invalid_range", "offset must be nonnegative, limit must be 1..16000")
        row = self.one("SELECT * FROM artifacts WHERE ref=?", (ref,))
        if not row:
            raise ATRError("not_found", "artifact is not a public run output")
        with self.object_path(ref).open("rb") as stream:
            stream.seek(offset)
            content = stream.read(limit)
        return {"ref": ref, "offset": offset, "next_offset": offset + len(content),
                "total_bytes": row["size"], "text": content.decode("utf-8", errors="replace"),
                "truncated": offset + len(content) < row["size"]}

    def emit(self, kind: str, now: datetime, *, task: str | None = None,
             run_id: str | None = None, tool_id: str | None = None,
             version: str | None = None, **data: Any) -> None:
        event = Event(event_id=uuid4().hex, kind=kind, timestamp=now,
                      root_task_id=task, run_id=run_id, tool_id=tool_id, version=version,
                      data=self.redactor.data(data))
        encoded = canonical(event)
        with self.transaction() as db:
            db.execute("INSERT INTO events VALUES (?,?,?,?,?,?)",
                       (event.event_id, kind, task, run_id, now.timestamp(), encoded))

    def export_events(self, path: Path) -> int:
        # Explicit operator action; no perpetual duplicate plaintext event log.
        rows = self.rows("SELECT data FROM events ORDER BY timestamp,id")
        with path.open("w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(row["data"] + "\n")
        return len(rows)

    def enabled(self, name: str) -> bool:
        row = self.one("SELECT value FROM switches WHERE name=?", (name,))
        if row is None:
            raise ATRError("invalid_switch", "unknown control switch")
        return bool(row["value"])

    def close(self) -> None:
        with self.lock:
            self.db.close()
