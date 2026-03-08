"""
SQLite helpers for jobs and API cache.
"""

import asyncio
import json
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

DB_PATH = Path("gh_profiler.db")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expires_iso(ttl_seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()


class Database:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self._write_lock: asyncio.Lock | None = None

    def _lock(self) -> asyncio.Lock:
        # Lazy-create so it binds to the running event loop
        if self._write_lock is None:
            self._write_lock = asyncio.Lock()
        return self._write_lock

    def _conn(self) -> sqlite3.Connection:
        import threading
        if not hasattr(threading.current_thread(), "_db_conn"):
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            threading.current_thread()._db_conn = conn
        return threading.current_thread()._db_conn

    def init(self):
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                input JSON NOT NULL,
                result JSON,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS api_cache (
                key TEXT PRIMARY KEY,
                value JSON NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
        """)
        conn.commit()

    # ── Jobs ─────────────────────────────────────────────────────────────────

    def _create_job_sync(self, job_type: str, input_data: dict) -> str:
        job_id = str(uuid.uuid4())
        now = _now_iso()
        self._conn().execute(
            "INSERT INTO jobs (id, type, status, input, result, created_at, updated_at) "
            "VALUES (?, ?, 'queued', ?, NULL, ?, ?)",
            (job_id, job_type, json.dumps(input_data), now, now),
        )
        self._conn().commit()
        return job_id

    async def create_job(self, job_type: str, input_data: dict) -> str:
        return await asyncio.get_event_loop().run_in_executor(
            None, self._create_job_sync, job_type, input_data
        )

    def _update_job_sync(self, job_id: str, status: str, result=None, error=None):
        self._conn().execute(
            "UPDATE jobs SET status=?, result=?, error=?, updated_at=? WHERE id=?",
            (status, json.dumps(result) if result is not None else None, error, _now_iso(), job_id),
        )
        self._conn().commit()

    async def update_job(self, job_id: str, status: str, result=None, error=None):
        async with self._lock():
            await asyncio.get_event_loop().run_in_executor(
                None, self._update_job_sync, job_id, status, result, error
            )

    def _get_job_sync(self, job_id: str) -> dict | None:
        row = self._conn().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["input"] = json.loads(d["input"]) if d["input"] else {}
        d["result"] = json.loads(d["result"]) if d["result"] else None
        return d

    async def get_job(self, job_id: str) -> dict | None:
        return await asyncio.get_event_loop().run_in_executor(None, self._get_job_sync, job_id)

    # ── Cache ─────────────────────────────────────────────────────────────────

    def _cache_get_sync(self, key: str) -> Any | None:
        row = self._conn().execute(
            "SELECT value, expires_at FROM api_cache WHERE key=?", (key,)
        ).fetchone()
        if not row:
            return None
        if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
            self._conn().execute("DELETE FROM api_cache WHERE key=?", (key,))
            self._conn().commit()
            return None
        value = row["value"]
        if isinstance(value, (str, bytes, bytearray)):
            return json.loads(value)
        return value  # SQLite returned a native type (int/float)

    async def cache_get(self, key: str) -> Any | None:
        return await asyncio.get_event_loop().run_in_executor(None, self._cache_get_sync, key)

    def _cache_set_sync(self, key: str, value: Any, ttl: int):
        self._conn().execute(
            "INSERT OR REPLACE INTO api_cache (key, value, expires_at) VALUES (?, ?, ?)",
            (key, json.dumps(value), _expires_iso(ttl)),
        )
        self._conn().commit()

    async def cache_set(self, key: str, value: Any, ttl: int):
        async with self._lock():
            await asyncio.get_event_loop().run_in_executor(None, self._cache_set_sync, key, value, ttl)


db = Database()
