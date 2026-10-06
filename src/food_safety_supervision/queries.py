"""督办查询：风险卡点、受限范围、全链缺证，以及公开/监管双视图。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from . import clock, graph
from .scheduler import pending_items
from .services import SupervisionService, closure_blockers

_STATUS_TEXT = {
    graph.C_OPEN: "办理中",
    graph.C_CLOSED: "已结案",
}


def _node_label(state: graph.GraphState, ref: str) -> str:
    subject = state.subjects.get(ref)
    if subject is not None:
        return f"{subject.name}（主体号 {subject.subject_no}）"
    node = state.nodes.get(ref)
    if node is not None:
        for key in ("title", "name", "listing_name", "batch_no", "license_no", "sample_no"):
            if node.attrs.get(key):
                return f"{node.attrs[key]}（{ref}）"
        return ref
    return ref


def _at(value: Optional[str | datetime]) -> datetime:
    if value is None:
        return clock.now()
    return clock.parse(value) if isinstance(value, str) else value


def stuck_points(service: SupervisionService, clue_ref: str, at: datetime) -> list[dict[str, Any]]:
    """风险当前卡住的环节与责任部门，按期限先后排列。"""
    state = service.state
    points: list[dict[str, Any]] = []
    for item in pending_items(state, at):
        if item.clue_ref != clue_ref:
            continue
        points.append({
            "agency_id": item.agency_id,
            "stage": item.stage,
            "stage_text": "待接收移送" if item.stage == "PENDING_ACCEPT" else "待整改/复查",
            "target": item.target,
            "deadline": item.deadline.isoformat(),
            "overdue": item.overdue,
        })
    for a in state.enforcements.values():
        if a.clue_ref == clue_ref and a.status == graph.A_OPEN:
            points.append({
                "agency_id": a.agency_id,
                "stage": "ENFORCEMENT_OPEN",
                "stage_text": "法定处置办理中" + (f"（已重开 {a.reopen_count} 次）" if a.reopen_count else ""),
                "target": a.ref,
                "deadline": None,
                "overdue": False,
            })
    return points


def active_scope_restrictions(service: SupervisionService, clue_ref: str,
                              at: datetime) -> list[dict[str, Any]]:
    state = service.state
    clue = state.clues[clue_ref]
    component = graph.neighbors(state, clue.subject_ref, at)
    rows = []
    for r in graph.active_restrictions(state, at):
        if r.clue_ref != clue_ref and r.scope_ref not in component:
            continue
        rows.append({
            "scope_type": r.scope_type,
            "scope_ref": r.scope_ref,
            "label": _node_label(state, r.scope_ref),
            "measure": "紧急下架/封存" if r.emergency else "限制流通",
            "reason": r.reason,
            "by_agency": r.by_agency,
            "restricted_at": r.restricted_at.isoformat(),
            "released": r.released and r.released_at is not None and r.released_at <= at,
            "released_at": r.released_at.isoformat() if r.released_at else None,
        })
    return sorted(rows, key=lambda row: row["restricted_at"])


def risk_status(service: SupervisionService, clue_ref: str,
                at: Optional[str | datetime] = None) -> dict[str, Any]:
    """一条风险的当前全貌：卡在哪个部门、哪些店铺/商品受限、全链闭合缺什么。"""
    ts = _at(at)
    state = service.state
    clue = state.clues.get(clue_ref)
    if clue is None:
        raise KeyError(f"线索不存在: {clue_ref}")
    blockers = closure_blockers(state, clue_ref, ts)
    return {
        "clue_ref": clue_ref,
        "status": clue.status,
        "status_text": _STATUS_TEXT.get(clue.status, clue.status),
        "subject_ref": clue.subject_ref,
        "subject_label": _node_label(state, clue.subject_ref),
        "reopen_count": clue.reopen_count,
        "stuck_at": stuck_points(service, clue_ref, ts),
        "restricted_scopes": active_scope_restrictions(service, clue_ref, ts),
        "closure_blockers": blockers,
        "chain_complete": not blockers and clue.status == graph.C_CLOSED,
        "as_of": ts.isoformat(),
    }


# -- 双视图 -----------------------------------------------------------------

def public_view(service: SupervisionService, clue_ref: str,
                at: Optional[str | datetime] = None) -> dict[str, Any]:
    """公开视图：只保留风险处置的社会可见信息，隐藏举报人与办案细节。"""
    status = risk_status(service, clue_ref, at)
    public_restrictions = []
    for row in status["restricted_scopes"]:
        public_restrictions.append({
            "scope_type": row["scope_type"],
            "label": row["label"],
            "measure": row["measure"],
            "in_effect": not row["released"],
        })
    if status["chain_complete"]:
        summary = "该风险已全链闭环处置"
    elif status["status"] == graph.C_CLOSED:
        summary = "已结案（后续出现新证据可能重新打开）"
    else:
        summary = "风险处置中，相关范围已采取控制措施" if public_restrictions else "风险处置中"
    return {
        "clue_ref": clue_ref,
        "subject_label": status["subject_label"],
        "status_text": status["status_text"],
        "summary": summary,
        "restricted_scopes": public_restrictions,
        "as_of": status["as_of"],
    }


def regulator_view(service: SupervisionService, clue_ref: str, viewer: str, *,
                   purpose: str = "查看风险全链", at: Optional[str | datetime] = None) -> dict[str, Any]:
    """监管视图：办案全细节，且每次访问都留痕。"""
    ts = _at(at)
    state = service.state
    clue = state.clues.get(clue_ref)
    if clue is None:
        raise KeyError(f"线索不存在: {clue_ref}")
    service._log_decision(viewer, clue_ref, "VIEW", purpose, ts)

    component = graph.neighbors(state, clue.subject_ref, ts)
    links = [
        {
            "link_type": l.link_type,
            "from_ref": l.from_ref,
            "to_ref": l.to_ref,
            "valid_from": l.valid_from.isoformat(),
            "valid_until": l.valid_until.isoformat() if l.valid_until else None,
        }
        for l in state.links
        if l.from_ref in component and l.to_ref in component
    ]
    evidence = [
        {
            "evidence_ref": e.ref,
            "kind": e.kind,
            "submitted_by": e.submitted_by,
            "status": e.status,
            "reviewer": e.reviewer,
            "superseded_by": e.superseded_by,
            "reject_reason": e.reject_reason,
        }
        for e in sorted(state.evidence.values(), key=lambda e: e.submitted_at)
        if e.clue_ref == clue_ref
    ]
    handoffs = [
        {
            "handoff_ref": h.ref,
            "from_agency": h.from_agency,
            "to_agency": h.to_agency,
            "status": h.status,
            "due_at": h.due_at.isoformat(),
            "accepted_at": h.accepted_at.isoformat() if h.accepted_at else None,
            "return_reason": h.return_reason,
        }
        for h in sorted(state.handoffs.values(), key=lambda h: h.created_at)
        if h.clue_ref == clue_ref
    ]
    enforcements = [
        {
            "action_ref": a.ref,
            "agency_id": a.agency_id,
            "status": a.status,
            "basis_refs": a.basis_refs,
            "resolution": a.resolution,
            "reopen_count": a.reopen_count,
            "reopen_reasons": a.reopen_reasons,
        }
        for a in sorted(state.enforcements.values(), key=lambda a: a.opened_at)
        if a.clue_ref == clue_ref
    ]
    rectifications = [
        {
            "rect_ref": rc.ref,
            "agency_id": rc.agency_id,
            "status": rc.status,
            "due_at": rc.due_at.isoformat(),
            "submitter": rc.submitter,
            "reviewer": rc.reviewer,
            "reject_reason": rc.reject_reason,
        }
        for rc in sorted(state.rectifications.values(), key=lambda rc: rc.requested_at)
        if rc.clue_ref == clue_ref
    ]
    escalations = [
        {
            "target": e.target,
            "agency_id": e.agency_id,
            "stage": e.stage,
            "deadline": e.deadline.isoformat(),
            "escalated_at": e.at.isoformat(),
        }
        for e in state.escalations
        if e.target in {h.ref for h in state.handoffs.values() if h.clue_ref == clue_ref}
        or e.target in {rc.ref for rc in state.rectifications.values() if rc.clue_ref == clue_ref}
    ]
    return {
        **risk_status(service, clue_ref, ts),
        "reporter": clue.reporter,
        "reporter_contact": clue.reporter_contact,
        "case_detail": clue.case_detail,
        "registered_at": clue.registered_at.isoformat(),
        "closed_by": clue.closed_by,
        "component_nodes": sorted(component),
        "links": sorted(links, key=lambda l: (l["valid_from"], l["link_type"])),
        "evidence": evidence,
        "handoffs": handoffs,
        "enforcements": enforcements,
        "rectifications": rectifications,
        "escalations": escalations,
    }
