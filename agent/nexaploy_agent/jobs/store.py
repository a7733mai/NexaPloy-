"""SQLite persistence for jobs and steps (§5.3).

WAL mode; one connection per thread (the API's thread pool, the worker and
the control-plane threads all share the file safely). All writes are short
autocommit statements except :meth:`claim_next`, which uses
``BEGIN IMMEDIATE`` so two workers can never take the same job.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..ulid import new_ulid

JOB_STATUSES = ("queued", "running", "success", "failure", "cancelled")
STEP_STATUSES = ("pending", "running", "success", "failure", "skipped")
FINAL_STATUSES = frozenset({"success", "failure", "cancelled"})
MAX_OUTPUT = 1_000_000  # chars kept per step (tail)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    params      TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL CHECK (status IN ('queued','running','success','failure','cancelled')),
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    ended_at    TEXT,
    error       TEXT,
    origin      TEXT NOT NULL DEFAULT 'local' CHECK (origin IN ('local','control_plane')),
    remote_id   TEXT,
    result      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs (status, created_at, id);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_remote ON jobs (origin, remote_id) WHERE remote_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS steps (
    job_id      TEXT NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('pending','running','success','failure','skipped')),
    output      TEXT NOT NULL DEFAULT '',
    started_at  TEXT,
    ended_at    TEXT,
    PRIMARY KEY (job_id, idx)
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JobNotFound(KeyError):
    pass


class JobStore:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        with self._init_lock:
            self._conn().executescript(SCHEMA)

    # ---- connection --------------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ---- serialisation ------------------------------------------------------------
    @staticmethod
    def _job(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["params"] = json.loads(d.get("params") or "{}")
        d["result"] = json.loads(d.get("result") or "{}")
        return d

    # ---- jobs ----------------------------------------------------------------------
    def create(self, kind: str, params: dict[str, Any], steps: Iterable[str],
               origin: str = "local", remote_id: str | None = None,
               status: str = "queued", error: str | None = None) -> dict[str, Any]:
        job_id = new_ulid()
        now = utcnow()
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO jobs (id, kind, params, status, created_at, origin, remote_id, error, ended_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, kind, json.dumps(params), status, now, origin, remote_id, error,
                 now if status in FINAL_STATUSES else None),
            )
            step_status = "skipped" if status in FINAL_STATUSES else "pending"
            conn.executemany(
                "INSERT INTO steps (job_id, idx, name, status) VALUES (?, ?, ?, ?)",
                [(job_id, i, name, step_status) for i, name in enumerate(steps)],
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return self.get(job_id)

    def get(self, job_id: str, with_steps: bool = True) -> dict[str, Any]:
        row = self._conn().execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFound(job_id)
        job = self._job(row)
        if with_steps:
            job["steps"] = self.steps(job_id)
        return job

    def find_remote(self, remote_id: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM jobs WHERE origin = 'control_plane' AND remote_id = ?", (remote_id,)
        ).fetchone()
        return self.get(row["id"]) if row else None

    def list(self, status: str | None = None, kind: str | None = None, limit: int = 50,
             offset: int = 0) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM jobs WHERE 1=1", []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        args += [int(limit), int(offset)]
        return [self._job(r) for r in self._conn().execute(sql, args).fetchall()]

    def claim_next(self) -> dict[str, Any] | None:
        """Atomically take the oldest queued job and mark it running."""
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at, id LIMIT 1"
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            conn.execute(
                "UPDATE jobs SET status = 'running', started_at = COALESCE(started_at, ?), error = NULL"
                " WHERE id = ?",
                (utcnow(), row["id"]),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return self.get(row["id"])

    def reset_stale(self) -> list[str]:
        """Re-queue jobs left ``running`` by a crash; running steps -> pending."""
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            ids = [r["id"] for r in conn.execute("SELECT id FROM jobs WHERE status = 'running'")]
            for job_id in ids:
                conn.execute("UPDATE jobs SET status = 'queued' WHERE id = ?", (job_id,))
                conn.execute(
                    "UPDATE steps SET status = 'pending', output = output || ? "
                    "WHERE job_id = ? AND status = 'running'",
                    ("\n[agent restarted; step will be retried]\n", job_id),
                )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return ids

    def finish(self, job_id: str, status: str, error: str | None = None) -> None:
        if status not in FINAL_STATUSES:
            raise ValueError(status)
        self._conn().execute(
            "UPDATE jobs SET status = ?, error = ?, ended_at = ? WHERE id = ?",
            (status, error, utcnow(), job_id),
        )

    def cancel(self, job_id: str) -> bool:
        """Cancel a queued job. Running jobs cannot be interrupted."""
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "UPDATE jobs SET status = 'cancelled', ended_at = ? WHERE id = ? AND status = 'queued'",
                (utcnow(), job_id),
            )
            if cur.rowcount:
                conn.execute(
                    "UPDATE steps SET status = 'skipped' WHERE job_id = ? AND status IN ('pending','running')",
                    (job_id,),
                )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return bool(cur.rowcount)

    def set_result(self, job_id: str, result: dict[str, Any]) -> None:
        self._conn().execute("UPDATE jobs SET result = ? WHERE id = ?", (json.dumps(result), job_id))

    def replace_params(self, job_id: str, params: dict[str, Any]) -> None:
        """Overwrite stored params (used to scrub secrets once a job is final)."""
        self._conn().execute("UPDATE jobs SET params = ? WHERE id = ?", (json.dumps(params), job_id))

    def delete_finished_before(self, iso_ts: str) -> int:
        cur = self._conn().execute(
            "DELETE FROM jobs WHERE status IN ('success','failure','cancelled') AND ended_at < ?",
            (iso_ts,),
        )
        return cur.rowcount

    # ---- steps ---------------------------------------------------------------------
    def steps(self, job_id: str) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT idx, name, status, output, started_at, ended_at FROM steps WHERE job_id = ? ORDER BY idx",
            (job_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def start_step(self, job_id: str, idx: int) -> None:
        self._conn().execute(
            "UPDATE steps SET status = 'running', started_at = ?, ended_at = NULL WHERE job_id = ? AND idx = ?",
            (utcnow(), job_id, idx),
        )

    def end_step(self, job_id: str, idx: int, status: str) -> None:
        if status not in STEP_STATUSES:
            raise ValueError(status)
        self._conn().execute(
            "UPDATE steps SET status = ?, ended_at = ? WHERE job_id = ? AND idx = ?",
            (status, utcnow(), job_id, idx),
        )

    def skip_remaining(self, job_id: str, after_idx: int) -> None:
        self._conn().execute(
            "UPDATE steps SET status = 'skipped' WHERE job_id = ? AND idx > ? AND status IN ('pending','running')",
            (job_id, after_idx),
        )

    def append_output(self, job_id: str, idx: int, text: str) -> None:
        if not text:
            return
        self._conn().execute(
            "UPDATE steps SET output = substr(output || ?, -?) WHERE job_id = ? AND idx = ?",
            (text, MAX_OUTPUT, job_id, idx),
        )
