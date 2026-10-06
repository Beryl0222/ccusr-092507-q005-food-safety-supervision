"""时效关系图投影：把事件流重放成当前状态。

投影是纯派生数据——进程崩溃或服务重启后用 EventStore.all_events()
重新 replay 即可完整恢复，期限、卡点、问责都以投影为准，不依赖内存定时器。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional

from . import clock

# 证据状态
EV_PENDING = "PENDING"
EV_APPROVED = "APPROVED"
EV_REJECTED = "REJECTED"
EV_SUPERSEDED = "SUPERSEDED"

# 移送状态
H_PENDING = "PENDING"
H_ACCEPTED = "ACCEPTED"
H_RETURNED = "RETURNED"

# 处置状态
A_OPEN = "OPEN"
A_RESOLVED = "RESOLVED"

# 整改状态
R_REQUESTED = "REQUESTED"
R_SUBMITTED = "SUBMITTED"
R_PASSED = "PASSED"
R_REJECTED = "REJECTED"

# 线索/案件状态
C_OPEN = "OPEN"
C_CLOSED = "CLOSED"


@dataclass
class SubjectState:
    ref: str
    subject_no: str
    name: str
    fingerprint: str
    registered_at: datetime
    quarantined: bool = False
    quarantine_reason: str = ""
    revisions: list[dict[str, Any]] = field(default_factory=list)
    verification: Optional[dict[str, Any]] = None


@dataclass
class NodeState:
    ref: str
    node_type: str
    registered_at: datetime
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass
class LinkState:
    link_type: str
    from_ref: str
    to_ref: str
    valid_from: datetime
    valid_until: Optional[datetime]
    clue_ref: Optional[str]
    event_id: str


@dataclass
class EvidenceState:
    ref: str
    kind: str
    submitted_by: str
    submitted_at: datetime
    clue_ref: Optional[str]
    status: str = EV_PENDING
    reviewer: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    reject_reason: str = ""
    superseded_by: Optional[str] = None


@dataclass
class HandoffState:
    ref: str
    from_agency: str
    to_agency: str
    created_at: datetime
    due_at: datetime
    clue_ref: Optional[str]
    status: str = H_PENDING
    accepted_at: Optional[datetime] = None
    handled_due_at: Optional[datetime] = None
    returned_at: Optional[datetime] = None
    return_reason: str = ""


@dataclass
class RestrictionState:
    scope_type: str
    scope_ref: str
    restricted_at: datetime
    by_agency: str
    reason: str
    emergency: bool
    clue_ref: Optional[str]
    released: bool = False
    released_at: Optional[datetime] = None
    release_reason: str = ""


@dataclass
class EnforcementState:
    ref: str
    agency_id: str
    opened_at: datetime
    basis_refs: list[str]
    clue_ref: Optional[str]
    scope: Optional[tuple[str, str]]
    status: str = A_OPEN
    resolution: str = ""
    resolved_at: Optional[datetime] = None
    reopen_count: int = 0
    reopen_reasons: list[str] = field(default_factory=list)


@dataclass
class RectificationState:
    ref: str
    agency_id: str
    requested_at: datetime
    due_at: datetime
    clue_ref: Optional[str]
    status: str = R_REQUESTED
    evidence_ref: Optional[str] = None
    submitted_at: Optional[datetime] = None
    reviewer: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    reject_reason: str = ""
    submitter: Optional[str] = None


@dataclass
class ClueState:
    ref: str
    subject_ref: str
    source_type: str
    registered_at: datetime
    reporter: Optional[str]
    reporter_contact: Optional[str]
    case_detail: str
    status: str = C_OPEN
    closed_at: Optional[datetime] = None
    closed_by: str = ""
    reopen_count: int = 0


@dataclass
class Escalation:
    target: str
    stage: str
    deadline: datetime
    at: datetime
    agency_id: str


@dataclass
class GraphState:
    subjects: dict[str, SubjectState] = field(default_factory=dict)
    nodes: dict[str, NodeState] = field(default_factory=dict)
    links: list[LinkState] = field(default_factory=list)
    clues: dict[str, ClueState] = field(default_factory=dict)
    evidence: dict[str, EvidenceState] = field(default_factory=dict)
    duties: dict[str, set[str]] = field(default_factory=dict)
    handoffs: dict[str, HandoffState] = field(default_factory=dict)
    restrictions: dict[tuple[str, str], RestrictionState] = field(default_factory=dict)
    enforcements: dict[str, EnforcementState] = field(default_factory=dict)
    rectifications: dict[str, RectificationState] = field(default_factory=dict)
    escalations: list[Escalation] = field(default_factory=list)
    versions: dict[tuple[str, str], int] = field(default_factory=dict)


def replay(events: list[Mapping[str, Any]]) -> GraphState:
    state = GraphState()
    for event in events:
        apply_event(state, event)
    return state


def _dt(event: Mapping[str, Any]) -> datetime:
    return clock.parse(event["occurred_at"])


def _ref(aggregate_type: str, aggregate_id: str) -> str:
    return f"{aggregate_type}:{aggregate_id}"


def apply_event(state: GraphState, event: Mapping[str, Any]) -> None:
    et = event["event_type"]
    p = event["payload"]
    at = _dt(event)
    agg = event["aggregate_type"]
    aid = event["aggregate_id"]
    state.versions[(agg, aid)] = max(state.versions.get((agg, aid), 0), event["version"])

    if et == "NODE_REGISTERED":
        ref = p.get("node_ref") or _ref(agg, aid)
        node = NodeState(
            ref=ref,
            node_type=p["node_type"],
            registered_at=at,
            attrs={k: v for k, v in p.items() if k not in ("node_type", "node_ref")},
        )
        state.nodes[ref] = node
        if p["node_type"] == "test_sample":
            node.attrs.setdefault("sample_ref", ref)

    elif et == "SUBJECT_REGISTERED":
        ref = _ref(agg, aid)
        state.subjects[ref] = SubjectState(
            ref=ref,
            subject_no=p["subject_no"],
            name=p["name"],
            fingerprint=p["profile_fingerprint"],
            registered_at=at,
        )
        state.nodes.setdefault(
            ref,
            NodeState(ref=ref, node_type="regulated_subject", registered_at=at,
                      attrs={"subject_no": p["subject_no"], "name": p["name"]}),
        )

    elif et == "SUBJECT_PROFILE_REVISED":
        ref = _ref(agg, aid)
        subject = state.subjects[ref]
        subject.revisions.append({
            "at": at,
            "old_fingerprint": subject.fingerprint,
            "new_fingerprint": p["profile_fingerprint"],
            "changed_fields": list(p.get("changed_fields", [])),
        })
        subject.fingerprint = p["profile_fingerprint"]
        # 同号但地址/许可证/内容指纹变化：先隔离，等待核验结论。
        subject.quarantined = True
        subject.quarantine_reason = "主体档案指纹变化（地址/许可证/页面内容），待隔离核验"

    elif et == "SUBJECT_VERIFIED":
        ref = _ref(agg, aid)
        subject = state.subjects[ref]
        subject.verification = {
            "at": at,
            "reviewer": p["reviewer"],
            "result": p["result"],
        }
        if p["result"] == "CONFIRMED":
            subject.quarantined = False
            subject.quarantine_reason = ""
        # REJECTED 时维持隔离，关联不得继续使用。

    elif et == "LINK_ESTABLISHED":
        state.links.append(LinkState(
            link_type=p["link_type"],
            from_ref=p["from_ref"],
            to_ref=p["to_ref"],
            valid_from=clock.parse(p["valid_from"]) if p.get("valid_from") else at,
            valid_until=clock.parse(p["valid_until"]) if p.get("valid_until") else None,
            clue_ref=p.get("clue_ref"),
            event_id=event["event_id"],
        ))

    elif et == "CLUE_REGISTERED":
        ref = _ref(agg, aid)
        state.clues[ref] = ClueState(
            ref=ref,
            subject_ref=p["subject_ref"],
            source_type=p["source_type"],
            registered_at=at,
            reporter=p.get("reporter"),
            reporter_contact=p.get("reporter_contact"),
            case_detail=p.get("case_detail", ""),
        )

    elif et == "EVIDENCE_SUBMITTED":
        ref = p["evidence_ref"]
        state.evidence[ref] = EvidenceState(
            ref=ref,
            kind=p["evidence_kind"],
            submitted_by=p["submitted_by"],
            submitted_at=at,
            clue_ref=p.get("clue_ref"),
        )

    elif et == "EVIDENCE_REVIEWED":
        ev = state.evidence[p["evidence_ref"]]
        ev.status = p["verdict"]
        ev.reviewer = p["reviewer"]
        ev.reviewed_at = at
        ev.reject_reason = p.get("reject_reason", "")

    elif et == "EVIDENCE_CORRECTED":
        old = state.evidence[p["corrected_ref"]]
        new_ref = p["new_evidence_ref"]
        old.status = EV_SUPERSEDED
        old.superseded_by = new_ref
        # 新证据作为待终审证据进入图（提交人不得自审，仍需 EVIDENCE_REVIEWED）。
        state.evidence[new_ref] = EvidenceState(
            ref=new_ref,
            kind=old.kind,
            submitted_by=p.get("submitted_by", old.submitted_by),
            submitted_at=at,
            clue_ref=old.clue_ref,
        )
        # 只重开真正依赖旧证据的处置。
        for action in state.enforcements.values():
            if p["corrected_ref"] in action.basis_refs and action.status == A_RESOLVED:
                action.status = A_OPEN
                action.reopen_count += 1
                action.reopen_reasons.append(
                    f"依据证据 {p['corrected_ref']} 被更正（{p.get('correction_reason', '')}）"
                )
                action.resolved_at = None
                action.resolution = ""
        # 已结案的案件随之重开。
        for clue in state.clues.values():
            if clue.status != C_CLOSED:
                continue
            affected = {
                a.clue_ref for a in state.enforcements.values()
                if p["corrected_ref"] in a.basis_refs
            }
            if clue.ref in affected or p.get("clue_ref") == clue.ref:
                clue.status = C_OPEN
                clue.reopen_count += 1
                clue.closed_at = None
                clue.closed_by = ""

    elif et == "DUTY_DECLARED":
        state.duties.setdefault(p["agency_id"], set()).add(p["duty_code"])

    elif et == "HANDOFF_CREATED":
        ref = _ref(agg, aid)
        state.handoffs[ref] = HandoffState(
            ref=ref,
            from_agency=p["from_agency"],
            to_agency=p["to_agency"],
            created_at=at,
            due_at=clock.parse(p["due_at"]),
            clue_ref=p.get("clue_ref"),
        )

    elif et == "HANDOFF_ACCEPTED":
        h = state.handoffs[_ref(agg, aid)]
        h.status = H_ACCEPTED
        h.accepted_at = at
        h.handled_due_at = clock.parse(p["due_at"])

    elif et == "HANDOFF_RETURNED":
        h = state.handoffs[_ref(agg, aid)]
        h.status = H_RETURNED
        h.returned_at = at
        h.return_reason = p["return_reason"]

    elif et == "SCOPE_RESTRICTED":
        key = (p["scope_type"], p["scope_ref"])
        state.restrictions[key] = RestrictionState(
            scope_type=p["scope_type"],
            scope_ref=p["scope_ref"],
            restricted_at=at,
            by_agency=p.get("agency_id", ""),
            reason=p.get("reason", ""),
            emergency=bool(p.get("emergency", False)),
            clue_ref=p.get("clue_ref"),
        )

    elif et == "SCOPE_RELEASED":
        key = (p["scope_type"], p["scope_ref"])
        r = state.restrictions[key]
        r.released = True
        r.released_at = at
        r.release_reason = p.get("reason", "")

    elif et == "ENFORCEMENT_OPENED":
        ref = _ref(agg, aid)
        existing = state.enforcements.get(ref)
        if existing is not None and existing.status == A_RESOLVED:
            # 更正级联后的重新立案：同一聚合继续追加版本。
            existing.status = A_OPEN
            existing.reopen_count += 1
            existing.reopen_reasons.append(p.get("reopen_reason", "依据变化重新立案"))
            existing.basis_refs = list(dict.fromkeys(existing.basis_refs + list(p.get("basis_refs", []))))
            return
        state.enforcements[ref] = EnforcementState(
            ref=ref,
            agency_id=p["agency_id"],
            opened_at=at,
            basis_refs=list(p.get("basis_refs", [])),
            clue_ref=p.get("clue_ref"),
            scope=(p["scope_type"], p["scope_ref"]) if p.get("scope_type") else None,
        )

    elif et == "ENFORCEMENT_RESOLVED":
        a = state.enforcements[_ref(agg, aid)]
        a.status = A_RESOLVED
        a.resolution = p["resolution"]
        a.resolved_at = at

    elif et == "RECTIFICATION_REQUESTED":
        ref = _ref(agg, aid)
        state.rectifications[ref] = RectificationState(
            ref=ref,
            agency_id=p["agency_id"],
            requested_at=at,
            due_at=clock.parse(p["due_at"]),
            clue_ref=p.get("clue_ref"),
        )

    elif et == "RECTIFICATION_SUBMITTED":
        r = state.rectifications[_ref(agg, aid)]
        r.status = R_SUBMITTED
        r.evidence_ref = p["evidence_ref"]
        r.submitted_at = at
        r.submitter = p.get("submitted_by")

    elif et == "RECTIFICATION_PASSED":
        r = state.rectifications[_ref(agg, aid)]
        r.status = R_PASSED
        r.reviewer = p["reviewer"]
        r.reviewed_at = at

    elif et == "RECTIFICATION_REJECTED":
        r = state.rectifications[_ref(agg, aid)]
        r.status = R_REJECTED
        r.reviewer = p["reviewer"]
        r.reviewed_at = at
        r.reject_reason = p["reject_reason"]

    elif et == "CASE_CLOSED":
        clue = state.clues[_ref(agg, aid)]
        clue.status = C_CLOSED
        clue.closed_at = at
        clue.closed_by = p["closed_by"]

    elif et == "CASE_REOPENED":
        clue = state.clues[_ref(agg, aid)]
        clue.status = C_OPEN
        clue.reopen_count += 1
        clue.closed_at = None
        clue.closed_by = ""

    elif et == "OVERDUE_ESCALATED":
        state.escalations.append(Escalation(
            target=_ref(agg, aid),
            stage=p["stage"],
            deadline=clock.parse(p["deadline"]),
            at=at,
            agency_id=p["agency_id"],
        ))


# -- 图查询辅助 -------------------------------------------------------------

def active_links(state: GraphState, at: datetime) -> list[LinkState]:
    return [
        l for l in state.links
        if l.valid_from <= at and (l.valid_until is None or l.valid_until > at)
    ]


def neighbors(state: GraphState, ref: str, at: Optional[datetime] = None) -> set[str]:
    """沿生效边无方向遍历连通分量。"""
    if at is None:
        links = state.links
    else:
        links = active_links(state, at)
    seen = {ref}
    frontier = [ref]
    while frontier:
        current = frontier.pop()
        for link in links:
            nxt = None
            if link.from_ref == current:
                nxt = link.to_ref
            elif link.to_ref == current:
                nxt = link.from_ref
            if nxt and nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    return seen


def active_restrictions(state: GraphState, at: Optional[datetime] = None) -> list[RestrictionState]:
    out = []
    for r in state.restrictions.values():
        if r.released and (at is None or r.released_at <= at):
            continue
        out.append(r)
    return out
