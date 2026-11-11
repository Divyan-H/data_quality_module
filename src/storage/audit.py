"""The audit database: who did what to which run, and every call made to the local model.

One SQLite file (`<data>/audit.db`, WAL mode) beside the runs folder. SQLite because the service runs on one host:
it needs no server, survives restarts, and is safe for the API process and the worker processes writing at once
(WAL allows one writer at a time; `busy_timeout` makes the others wait instead of failing). The schema is versioned
with `PRAGMA user_version` and migrated forward on open; rows are only ever inserted, never updated or deleted by
the application (deleting a run leaves its audit rows: that a deletion happened is itself the record).

What is stored: identifiers, action names, counts, model names, timings. What is never stored: cell values, file
contents, prompts, model answers, access tokens.

Tables (docs/DATABASE.md has the full description):
    audit_events(id, at, actor, action, run_id, phase, request_id, detail)   detail = small JSON object
    llm_calls(id, at, run_id, phase, purpose, model, digest, prompt_version, latency_ms, outcome,
              prompt_chars, answer_chars, attempt)
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from src.observability.context import request_id

log = logging.getLogger("dq.audit")
BUSY_TIMEOUT_MS = 5000
MAX_DETAIL_CHARS = 4000

# Each entry moves the schema one version forward. Never edit an entry that has shipped; append a new one.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE audit_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        at          TEXT    NOT NULL,
        actor       TEXT    NOT NULL,
        action      TEXT    NOT NULL,
        run_id      TEXT,
        phase       TEXT,
        request_id  TEXT,
        detail      TEXT    NOT NULL DEFAULT '{}'
    );
    CREATE INDEX audit_events_run ON audit_events (run_id, id);
    CREATE INDEX audit_events_at  ON audit_events (at);
    CREATE TABLE llm_calls (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        at             TEXT    NOT NULL,
        run_id         TEXT,
        phase          TEXT,
        purpose        TEXT    NOT NULL,
        model          TEXT    NOT NULL,
        digest         TEXT,
        prompt_version TEXT,
        latency_ms     INTEGER NOT NULL,
        outcome        TEXT    NOT NULL CHECK (outcome IN ('ok', 'invalid', 'no_answer', 'repaired')),
        prompt_chars   INTEGER NOT NULL DEFAULT 0,
        answer_chars   INTEGER NOT NULL DEFAULT 0,
        attempt        INTEGER NOT NULL DEFAULT 1
    );
    CREATE INDEX llm_calls_run ON llm_calls (run_id, id);
    """,
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class AuditLog:
    """Append-only access to the audit database. One instance per process; safe to share between threads."""

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock, closing(self._connect()) as con:
                self._migrate(con)

    # ------------------------------------------------------------------ connection and schema
    def _connect(self) -> sqlite3.Connection:
        assert self.path is not None
        con = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        con.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA synchronous = NORMAL")           # durable across process crashes; fsync at checkpoints
        con.row_factory = sqlite3.Row
        return con

    @staticmethod
    def _migrate(con: sqlite3.Connection) -> None:
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if version > len(MIGRATIONS):
            raise RuntimeError(f"audit.db is at schema {version}, newer than this build understands ({len(MIGRATIONS)}). "
                               "Upgrade the service or restore the matching backup.")
        for n in range(version, len(MIGRATIONS)):
            con.execute("BEGIN IMMEDIATE")                    # two processes starting together: one migrates, one waits
            try:
                if con.execute("PRAGMA user_version").fetchone()[0] == n:
                    for statement in MIGRATIONS[n].split(";"):
                        if statement.strip():
                            con.execute(statement)
                    con.execute(f"PRAGMA user_version = {n + 1}")
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise

    def schema_version(self) -> int:
        if self.path is None:
            return 0
        with self._lock, closing(self._connect()) as con:
            return con.execute("PRAGMA user_version").fetchone()[0]

    # ------------------------------------------------------------------ writes (never raise into the caller)
    def _insert(self, sql: str, values: tuple) -> None:
        if self.path is None:
            return
        try:
            with self._lock, closing(self._connect()) as con:
                con.execute(sql, values)
        except sqlite3.Error:
            # Losing an audit row is serious but must not lose the analyst's work: say so loudly, carry on.
            log.exception("audit write failed")

    def record(self, action: str, run_id: str | None = None, phase: str | None = None, actor: str = "analyst",
               **detail) -> None:
        """One event. `detail` holds small, value-free facts (counts, names of operations, file names)."""
        text = json.dumps(detail, ensure_ascii=False, default=str)
        if len(text) > MAX_DETAIL_CHARS:
            text = json.dumps({"truncated": True, "keys": sorted(detail)})
        self._insert("INSERT INTO audit_events (at, actor, action, run_id, phase, request_id, detail) VALUES (?,?,?,?,?,?,?)",
                     (_now(), actor, action, run_id, phase, request_id.get(), text))

    def llm_call(self, *, purpose: str, model: str, latency_ms: int, outcome: str, run_id: str | None = None,
                 phase: str | None = None, digest: str | None = None, prompt_version: str | None = None,
                 prompt_chars: int = 0, answer_chars: int = 0, attempt: int = 1) -> None:
        self._insert("INSERT INTO llm_calls (at, run_id, phase, purpose, model, digest, prompt_version, latency_ms, outcome, "
                     "prompt_chars, answer_chars, attempt) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (_now(), run_id, phase, purpose, model, digest, prompt_version, int(latency_ms), outcome,
                      int(prompt_chars), int(answer_chars), int(attempt)))

    # ------------------------------------------------------------------ reads
    def _rows(self, sql: str, values: tuple) -> list[dict]:
        if self.path is None:
            return []
        with self._lock, closing(self._connect()) as con:
            return [dict(r) for r in con.execute(sql, values).fetchall()]

    def events(self, run_id: str | None = None, limit: int = 200, before_id: int | None = None) -> list[dict]:
        """Newest first. `before_id` pages backwards."""
        where, values = [], []
        if run_id:
            where.append("run_id = ?")
            values.append(run_id)
        if before_id:
            where.append("id < ?")
            values.append(before_id)
        sql = ("SELECT id, at, actor, action, run_id, phase, request_id, detail FROM audit_events"
               + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?")
        rows = self._rows(sql, (*values, max(1, min(int(limit), 1000))))
        for r in rows:
            r["detail"] = json.loads(r["detail"])
        return rows

    def llm_calls(self, run_id: str | None = None, limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM llm_calls" + (" WHERE run_id = ?" if run_id else "") + " ORDER BY id DESC LIMIT ?"
        return self._rows(sql, ((run_id,) if run_id else ()) + (max(1, min(int(limit), 1000)),))

    def llm_summary(self) -> dict:
        """Totals for the metrics endpoint and the evaluation report."""
        rows = self._rows("SELECT purpose, outcome, COUNT(*) AS n, CAST(AVG(latency_ms) AS INTEGER) AS avg_ms "
                          "FROM llm_calls GROUP BY purpose, outcome", ())
        return {"by_purpose_outcome": rows, "total": sum(r["n"] for r in rows)}
