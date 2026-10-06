"""领域服务抛出的错误。"""

from __future__ import annotations


class DomainError(Exception):
    """所有业务规则冲突的基类，code 供调用方稳定分支判断。"""

    code = "domain_error"


class ContractViolation(DomainError):
    code = "contract_violation"

    def __init__(self, issues: list[str]) -> None:
        super().__init__("；".join(issues))
        self.issues = issues


class DuplicateEvent(DomainError):
    code = "duplicate_event"

    def __init__(self, event_id: str) -> None:
        super().__init__(f"事件标识已存在且载荷不一致: {event_id}")
        self.event_id = event_id


class VersionConflict(DomainError):
    code = "version_conflict"

    def __init__(self, aggregate_type: str, aggregate_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"聚合 {aggregate_type}/{aggregate_id} 版本冲突：期望 {expected}，实际 {actual}"
        )
        self.expected = expected
        self.actual = actual


class JurisdictionError(DomainError):
    code = "jurisdiction_error"


class ReviewerConflict(DomainError):
    code = "reviewer_conflict"


class HandoffClosed(DomainError):
    code = "handoff_closed"


class IllegalState(DomainError):
    code = "illegal_state"
