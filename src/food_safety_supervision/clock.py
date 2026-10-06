"""时间解析：领域内只接受显式带时区的时间。"""

from __future__ import annotations

from datetime import datetime, timezone


def parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"时间必须携带时区: {value}")
    return parsed


def now() -> datetime:
    return datetime.now(timezone.utc)


def format_(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("时间必须携带时区")
    return value.isoformat()
