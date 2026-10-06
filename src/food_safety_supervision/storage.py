"""SQLite 事件库：仅追加事件流 + 监管访问审计。

故障恢复语义：所有状态都在事件流里，重放即恢复；逾期催办以投影中的
deadline 为准，不依赖进程内定时器。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .errors import DuplicateConflict

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id       TEXT NOT NULL UNIQUE,
    event_type     TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id   TEXT NOT NULL,
    occurred_at    TEXT NOT NULL,
    version        INTEGER NOT NULL,
    payload        TEXT NOT NULL,
    body           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_agg ON events(aggregate_type, aggregate_id, version);
CREATE INDEX IF NOT EXISTS events_time ON events(occurred_at);

CREATE TABLE IF NOT EXISTS access_log (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    accessed_at TEXT NOT NULL,
    viewer     TEXT NOT NULL,
    view       TEXT NOT NULL,
    target     TEXT NOT NULL,
    decision   TEXT NOT NULL,
    detail     TEXT NOT NULL
);
"""


class EventStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "EventStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 写入 -------------------------------------------------------------

    def append(self, event: Mapping[str, Any]) -> bool:
        """追加事件。

        返回 True 表示新写入；False 表示同一 event_id 重放且内容完全一致（幂等跳过）。
        内容不一致抛 DuplicateConflict——同号回调内容变化必须隔离核验，不允许覆盖。
        """
        body = json.dumps(event, ensure_ascii=False, sort_keys=True)
        existing = self._conn.execute(
            "SELECT body FROM events WHERE event_id = ?", (event["event_id"],)
        ).fetchone()
        if existing is not None:
            if existing["body"] != body:
                raise DuplicateConflict(
                    f"事件 {event['event_id']} 已存在但内容不同；回调同号冲突需隔离核验"
                )
            return False
        self._conn.execute(
            "INSERT INTO events (event_id, event_type, aggregate_type, aggregate_id,"
            " occurred_at, version, payload, body) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event["event_id"],
                event["event_type"],
                event["aggregate_type"],
                event["aggregate_id"],
                event["occurred_at"],
                event["version"],
                json.dumps(event["payload"], ensure_ascii=False, sort_keys=True),
                body,
            ),
        )
        self._conn.commit()
        return True

    # -- 读取 -------------------------------------------------------------

    def all_events(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT body FROM events ORDER BY seq"
        ).fetchall()
        return [json.loads(row["body"]) for row in rows]

    def events_for(self, aggregate_type: str, aggregate_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT body FROM events WHERE aggregate_type = ? AND aggregate_id = ?"
            " ORDER BY seq",
            (aggregate_type, aggregate_id),
        ).fetchall()
        return [json.loads(row["body"]) for row in rows]

    def has_event(self, event_id: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM events WHERE event_id = ?", (event_id,)
        ).fetchone() is not None

    # -- 监管访问留痕 ------------------------------------------------------

    def log_access(
        self,
        *,
        accessed_at: str,
        viewer: str,
        view: str,
        target: str,
        decision: str,
        detail: str = "",
    ) -> None:
        self._conn.execute(
            "INSERT INTO access_log (accessed_at, viewer, view, target, decision, detail)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (accessed_at, viewer, view, target, decision, detail),
        )
        self._conn.commit()

    def access_log(self, viewer: Optional[str] = None) -> list[dict[str, str]]:
        if viewer is None:
            rows = self._conn.execute(
                "SELECT * FROM access_log ORDER BY seq"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM access_log WHERE viewer = ? ORDER BY seq", (viewer,)
            ).fetchall()
        return [dict(row) for row in rows]

    def replay(self) -> Iterable[Mapping[str, Any]]:
        return self.all_events()
