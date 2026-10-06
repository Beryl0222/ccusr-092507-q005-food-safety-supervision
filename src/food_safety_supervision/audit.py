"""监管审计日志：区分决定与访问两类记录，落盘并可在崩溃后重放。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuditLog:
    """JSONL 审计台账。

    - 决定类：部门确认、移送接收/退回、终审、紧急处置、逾期问责等改变状态的动作；
    - 访问类：监管视图的每次查询，记录访问人及查询对象。
    崩溃产生的残缺末行在重新打开时截断丢弃。
    """

    DECISION = "decision"
    ACCESS = "access"

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._entries: list[dict[str, Any]] = []
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._replay()

    def _replay(self) -> None:
        assert self._path is not None
        if not self._path.exists():
            return
        data = self._path.read_bytes()
        good_end = 0
        offset = 0
        lines = data.splitlines(keepends=True)
        for index, raw in enumerate(lines):
            line = raw.strip()
            offset += len(raw)
            if not line:
                good_end = offset
                continue
            try:
                self._entries.append(json.loads(line.decode("utf-8")))
            except (json.JSONDecodeError, UnicodeDecodeError):
                if index == len(lines) - 1:
                    with self._path.open("r+b") as handle:
                        handle.truncate(good_end)
                    break
                raise
            good_end = offset

    def record(
        self,
        kind: str,
        actor: str,
        action: str,
        target: str | None = None,
        detail: dict[str, Any] | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        entry = {
            "at": at or now_iso(),
            "kind": kind,
            "actor": actor,
            "action": action,
            "target": target,
            "detail": detail or {},
        }
        if self._path is not None:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        self._entries.append(entry)
        return dict(entry)

    def decision(self, actor: str, action: str, target: str | None = None, **detail: Any) -> dict[str, Any]:
        return self.record(self.DECISION, actor, action, target, detail)

    def access(self, actor: str, action: str, target: str | None = None, **detail: Any) -> dict[str, Any]:
        return self.record(self.ACCESS, actor, action, target, detail)

    def entries(self, kind: str | None = None) -> list[dict[str, Any]]:
        return [dict(entry) for entry in self._entries if kind is None or entry["kind"] == kind]
