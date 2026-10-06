"""协同督办领域错误。"""

from __future__ import annotations


class SupervisionError(Exception):
    """所有业务规则冲突的基类，code 供上层稳定分支判断。"""

    code = "supervision_error"


class ContractViolation(SupervisionError):
    code = "contract_violation"

    def __init__(self, issues: list[str]):
        super().__init__("；".join(issues))
        self.issues = issues


class DuplicateConflict(SupervisionError):
    """同一 event_id 重放但内容不一致。"""

    code = "duplicate_conflict"


class VersionConflict(SupervisionError):
    code = "version_conflict"


class OutOfJurisdiction(SupervisionError):
    code = "out_of_jurisdiction"


class ReviewerConflict(SupervisionError):
    code = "reviewer_conflict"


class HandoffStateError(SupervisionError):
    code = "handoff_state_error"


class SubjectQuarantined(SupervisionError):
    code = "subject_quarantined"


class ClosureBlocked(SupervisionError):
    code = "closure_blocked"

    def __init__(self, blockers: list[str]):
        super().__init__("全链尚未闭合：" + "；".join(blockers))
        self.blockers = blockers


class UnknownReference(SupervisionError):
    code = "unknown_reference"
