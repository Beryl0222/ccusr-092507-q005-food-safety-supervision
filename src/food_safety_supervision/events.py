"""JSONL 事件存储：幂等追加、版本检查与崩溃重放。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .errors import DuplicateEvent, VersionConflict


def _canonical(event: Mapping[str, Any]) -> str:
    """稳定序列化事件内容。

    版本号由存储层按聚合链分配：同一事件标识的重放可能带着链上的新版本号，
    因此比较内容时剔除 ``version``，其余字段任一不同即视为标识冲突。
    """
    body = {k: v for k, v in event.items() if k != "version"}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class EventStore:
    """每个聚合一条版本链；同一 event_id 重放为无操作。

    传 ``path=None`` 时仅存于内存。落盘时每次追加都 flush + fsync；
    重新打开若末行在崩溃中被截断，忽略该残缺行后继续重放。
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._events: list[dict[str, Any]] = []
        self._seen: dict[str, str] = {}
        self._versions: dict[tuple[str, str], int] = {}
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._replay()

    def _replay(self) -> None:
        assert self._path is not None
        if not self._path.exists():
            return
        data = self._path.read_bytes()
        offset = 0
        good_end = 0
        lines = data.splitlines(keepends=True)
        for index, raw in enumerate(lines):
            line = raw.strip()
            if not line:
                offset += len(raw)
                good_end = offset
                continue
            try:
                event = json.loads(line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                if index == len(lines) - 1:
                    # 崩溃时写了一半的末行：截断丢弃，调用方按原状态重试。
                    with self._path.open("r+b") as handle:
                        handle.truncate(good_end)
                    break
                raise
            self._ingest(event)
            offset += len(raw)
            good_end = offset

    def _ingest(self, event: Mapping[str, Any]) -> None:
        self._seen[event["event_id"]] = _canonical(event)
        key = (event["aggregate_type"], event["aggregate_id"])
        self._versions[key] = event["version"]
        self._events.append(dict(event))

    def has_event(self, event_id: str) -> bool:
        return event_id in self._seen

    def append(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """追加事件；同标识同内容重放返回原事件，同标识异内容则报错。"""
        event_id = event["event_id"]
        canonical = _canonical(event)
        if event_id in self._seen:
            if self._seen[event_id] != canonical:
                raise DuplicateEvent(event_id)
            return self._find(event_id)

        key = (event["aggregate_type"], event["aggregate_id"])
        next_version = self._versions.get(key, 0) + 1
        if event["version"] != next_version:
            raise VersionConflict(
                key[0], key[1], expected=next_version, actual=event["version"]
            )

        stored = dict(event)
        line = json.dumps(stored, ensure_ascii=False) + "\n"
        if self._path is not None:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                import os

                os.fsync(handle.fileno())
        self._ingest(stored)
        return stored

    def _find(self, event_id: str) -> dict[str, Any]:
        for event in self._events:
            if event["event_id"] == event_id:
                return event
        raise KeyError(event_id)

    def events(
        self, aggregate_type: str | None = None, aggregate_id: str | None = None
    ) -> list[dict[str, Any]]:
        result = self._events
        if aggregate_type is not None:
            result = [
                event
                for event in result
                if event["aggregate_type"] == aggregate_type
                and (aggregate_id is None or event["aggregate_id"] == aggregate_id)
            ]
        return [dict(event) for event in result]

    def version(self, aggregate_type: str, aggregate_id: str) -> int:
        return self._versions.get((aggregate_type, aggregate_id), 0)

    def all_events(self) -> list[dict[str, Any]]:
        return [dict(event) for event in self._events]
