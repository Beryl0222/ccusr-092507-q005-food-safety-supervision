"""逾期督办：按原期限推进待接收、待复查与逾期问责。

期限全部来自事件（HANDOFF_CREATED.due_at、HANDOFF_ACCEPTED.due_at、
RECTIFICATION_REQUESTED.due_at）。调度器本身不保存状态——故障恢复后
重跑只会为尚未问责的逾期项补记一次 OVERDUE_ESCALATED，因此可安全重放。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from . import clock, graph
from .services import SupervisionService

STAGE_ACCEPT = "PENDING_ACCEPT"
STAGE_RECTIFY = "PENDING_REVIEW"


@dataclass(frozen=True)
class PendingItem:
    stage: str
    target: str
    agency_id: str
    clue_ref: Optional[str]
    deadline: datetime
    overdue: bool


def pending_items(state: graph.GraphState, at: datetime) -> list[PendingItem]:
    items: list[PendingItem] = []
    for h in state.handoffs.values():
        if h.status != graph.H_PENDING:
            continue
        items.append(PendingItem(
            stage=STAGE_ACCEPT, target=h.ref, agency_id=h.to_agency,
            clue_ref=h.clue_ref, deadline=h.due_at, overdue=h.due_at < at,
        ))
    for rc in state.rectifications.values():
        if rc.status in (graph.R_REQUESTED, graph.R_SUBMITTED):
            items.append(PendingItem(
                stage=STAGE_RECTIFY, target=rc.ref, agency_id=rc.agency_id,
                clue_ref=rc.clue_ref, deadline=rc.due_at, overdue=rc.due_at < at,
            ))
    return sorted(items, key=lambda item: item.deadline)


def escalate_overdue(service: SupervisionService, at: Optional[str | datetime] = None) -> list[str]:
    """对所有已逾期且尚未问责的事项记一次 OVERDUE_ESCALATED。

    返回本次新问责的目标列表；重复调用（含故障恢复后的重跑）返回空或仅新增项，
    已问责事项不会被重复问责。
    """
    ts = service._at(at)
    already = {(e.target, e.stage) for e in service.state.escalations}
    escalated: list[str] = []
    for item in pending_items(service.state, ts):
        if not item.overdue or (item.target, item.stage) in already:
            continue
        if item.stage == STAGE_ACCEPT:
            agg_type, agg_id = "agency_handoff", item.target.split(":", 1)[1]
        else:
            agg_type, agg_id = "rectification", item.target.split(":", 1)[1]
        service._emit(
            "OVERDUE_ESCALATED", agg_type, agg_id,
            {"agency_id": item.agency_id, "stage": item.stage,
             "deadline": item.deadline.isoformat()},
            ts,
        )
        service._log_decision(
            "food_safety_office", item.target, "OVERDUE_ESCALATED",
            f"{item.agency_id} 在 {item.stage} 环节超过期限 {item.deadline.isoformat()}，启动问责",
            ts,
        )
        escalated.append(item.target)
    return escalated
