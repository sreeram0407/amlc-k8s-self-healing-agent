"""SQLite-backed audit log for agent decisions."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_id TEXT,
    pod_name TEXT,
    namespace TEXT,
    event_type TEXT,
    diagnosis TEXT,
    action_taken TEXT,
    action_params TEXT,
    guardrail_check TEXT,
    outcome TEXT,
    llm_reasoning TEXT,
    tokens_used INTEGER
);
"""


class AuditLogger:
    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def log(self, entry: dict[str, Any]) -> int:
        ts = entry.get("timestamp") or datetime.now(timezone.utc).isoformat()
        params = entry.get("action_params")
        if isinstance(params, (dict, list)):
            params = json.dumps(params, default=str)

        cur = self._conn.execute(
            """
            INSERT INTO audit_log (
                timestamp, event_id, pod_name, namespace, event_type,
                diagnosis, action_taken, action_params, guardrail_check,
                outcome, llm_reasoning, tokens_used
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                entry.get("event_id", ""),
                entry.get("pod_name", ""),
                entry.get("namespace", ""),
                entry.get("event_type", ""),
                entry.get("diagnosis", ""),
                entry.get("action_taken", ""),
                params or "",
                entry.get("guardrail_check", ""),
                entry.get("outcome", ""),
                entry.get("llm_reasoning", ""),
                int(entry.get("tokens_used") or 0),
            ),
        )
        self._conn.commit()
        return cur.lastrowid or 0

    def get_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def close(self) -> None:
        self._conn.close()
