"""协同督办领域服务。

所有写操作都先过交换层契约校验，再追加事件并更新内存投影；
进程重启后从事件流重放恢复。命令默认按聚合分配连续版本号，
事件标识由 类型+聚合+版本 确定性生成，因此同一命令重试与平台
回调重放都天然幂等；同号不同内容一律按冲突拒绝。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

from . import clock, graph
from .contracts import validate_event
from .errors import (
    ClosureBlocked,
    ContractViolation,
    HandoffStateError,
    OutOfJurisdiction,
    ReviewerConflict,
    SubjectQuarantined,
    UnknownReference,
)
from .storage import EventStore

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"


def load_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def aref(aggregate_type: str, value: str) -> str:
    """把裸编号补成稳定的图引用；已是 类型:编号 形式时原样返回。"""
    return value if ":" in value else f"{aggregate_type}:{value}"


def closure_blockers(state: graph.GraphState, clue_ref: str, at: datetime) -> list[str]:
    """全链结案闸口：返回尚不满足的闭合条件（空列表方可结案）。"""
    clue = state.clues.get(clue_ref)
    if clue is None:
        raise UnknownReference(f"线索不存在: {clue_ref}")
    blockers: list[str] = []
    component = graph.neighbors(state, clue.subject_ref, at)

    for sref in sorted(component):
        subject = state.subjects.get(sref)
        if subject is not None and subject.quarantined:
            blockers.append(
                f"主体 {subject.name}（{subject.subject_no}）档案指纹变化后仍处隔离核验，关联暂不可用"
            )

    for h in sorted(state.handoffs.values(), key=lambda x: x.created_at):
        if h.clue_ref == clue_ref and h.status == graph.H_PENDING:
            blockers.append(
                f"移送 {h.ref} 正待 {h.to_agency} 接收或退回（接收期限 {h.due_at.isoformat()}）"
            )

    evs = sorted(
        (e for e in state.evidence.values() if e.clue_ref == clue_ref),
        key=lambda e: e.submitted_at,
    )
    for e in evs:
        if e.status == graph.EV_PENDING:
            blockers.append(f"证据 {e.ref}（{e.kind}）提交后尚未经终审")
        elif e.status == graph.EV_REJECTED:
            blockers.append(f"证据 {e.ref}（{e.kind}）终审未通过，需补证或更正")
        elif e.status == graph.EV_SUPERSEDED:
            successor = state.evidence.get(e.superseded_by or "")
            if successor is None or successor.status != graph.EV_APPROVED:
                blockers.append(
                    f"证据 {e.ref} 已被 {e.superseded_by} 更正，但新结论尚未终审通过"
                )

    actions = [a for a in state.enforcements.values() if a.clue_ref == clue_ref]
    for a in sorted(actions, key=lambda x: x.opened_at):
        if a.status == graph.A_OPEN:
            tail = f"（已重开 {a.reopen_count} 次）" if a.reopen_count else ""
            blockers.append(f"法定处置 {a.ref} 仍在 {a.agency_id} 办理中{tail}")

    for r in graph.active_restrictions(state, at):
        if r.clue_ref != clue_ref and r.scope_ref not in component:
            continue
        label = f"{r.scope_type}:{r.scope_ref}"
        released_now = r.released and r.released_at is not None and r.released_at <= at
        if not released_now:
            kind = "紧急" if r.emergency else "常规"
            blockers.append(f"{kind}限制范围 {label} 仍在限制流通，尚未正式解除")
        if r.emergency:
            covered = any(
                a.status == graph.A_RESOLVED
                and a.scope == (r.scope_type, r.scope_ref)
                for a in actions
            )
            if not covered:
                blockers.append(
                    f"{label} 的紧急下架/封存后缺少覆盖该范围的法定处置结论，紧急措施不能替代法定程序"
                )

    for rc in sorted(state.rectifications.values(), key=lambda x: x.requested_at):
        if rc.clue_ref != clue_ref:
            continue
        if rc.status == graph.R_REQUESTED:
            blockers.append(f"整改 {rc.ref} 尚未提交整改材料（期限 {rc.due_at.isoformat()}）")
        elif rc.status == graph.R_SUBMITTED:
            blockers.append(f"整改 {rc.ref} 已提交，{rc.agency_id} 尚未复查")
        elif rc.status == graph.R_REJECTED:
            blockers.append(f"整改 {rc.ref} 复查未通过：{rc.reject_reason}")

    return blockers


class SupervisionService:
    def __init__(self, store: EventStore, schema: Optional[Mapping[str, Any]] = None) -> None:
        self.store = store
        self.schema = dict(schema or load_schema())
        self.state = graph.replay(store.all_events())

    # -- 内部 -------------------------------------------------------------

    def _at(self, value: Optional[str | datetime]) -> datetime:
        if value is None:
            return clock.now()
        return clock.parse(value) if isinstance(value, str) else value

    def _emit(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: Mapping[str, Any],
        at: datetime,
        event_id: Optional[str] = None,
    ) -> tuple[dict[str, Any], bool]:
        version = self.state.versions.get((aggregate_type, aggregate_id), 0) + 1
        event = {
            "event_id": event_id or f"{event_type}:{aggregate_type}:{aggregate_id}:v{version}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": at.isoformat(),
            "version": version,
            "payload": dict(payload),
        }
        issues = validate_event(event, self.schema)
        if issues:
            raise ContractViolation(
                [f"{i.field} {i.code} {i.message}" for i in issues]
            )
        appended = self.store.append(event)
        if appended:
            graph.apply_event(self.state, event)
        return event, appended

    def _log_decision(self, viewer: str, target: str, decision: str, detail: str = "",
                      at: Optional[datetime] = None) -> None:
        self.store.log_access(
            accessed_at=(at or clock.now()).isoformat(),
            viewer=viewer,
            view="REGULATOR",
            target=target,
            decision=decision,
            detail=detail,
        )

    def _require_agency(self, agency_id: str) -> None:
        if agency_id not in self.state.duties:
            raise OutOfJurisdiction(f"{agency_id} 尚未声明任何法定职责，不能确认事实或作出处置")

    def _assert_may_handle(self, agency_id: str, clue_ref: str) -> None:
        """只能在职责范围内、且移送已被本部门接收后行事。"""
        self._require_agency(agency_id)
        for h in self.state.handoffs.values():
            if h.clue_ref != clue_ref or h.to_agency != agency_id:
                continue
            if h.status == graph.H_PENDING:
                raise OutOfJurisdiction(f"移送 {h.ref} 尚未被 {agency_id} 接收")
            if h.status == graph.H_RETURNED:
                raise OutOfJurisdiction(f"移送 {h.ref} 已被 {agency_id} 退回，不能据此行事")

    def _require_subject(self, ref: str) -> graph.SubjectState:
        subject = self.state.subjects.get(ref)
        if subject is None:
            raise UnknownReference(f"经营主体不存在: {ref}")
        return subject

    def _require_not_quarantined(self, ref: str) -> None:
        subject = self.state.subjects.get(ref)
        if subject is not None and subject.quarantined:
            raise SubjectQuarantined(
                f"主体 {subject.name} 档案指纹变化后处于隔离核验，确认前不得沿既有关系继续关联"
            )

    # -- 主体与图 ----------------------------------------------------------

    def register_subject(
        self, subject_id: str, *, subject_no: str, name: str, profile_fingerprint: str,
        at: Optional[str | datetime] = None, event_id: Optional[str] = None,
    ) -> str:
        ts = self._at(at)
        self._emit(
            "SUBJECT_REGISTERED", "regulated_subject", subject_id,
            {"subject_no": subject_no, "name": name,
             "profile_fingerprint": profile_fingerprint},
            ts, event_id,
        )
        return aref("regulated_subject", subject_id)

    def revise_subject_profile(
        self, subject_ref: str, *, profile_fingerprint: str, changed_fields: list[str],
        at: Optional[str | datetime] = None,
    ) -> str:
        """主体号相同但地址/许可证/内容指纹变化：档案进入隔离核验。"""
        ref = self._require_subject(subject_ref)
        _, agg_id = subject_ref.split(":", 1)
        self._emit(
            "SUBJECT_PROFILE_REVISED", "regulated_subject", agg_id,
            {"subject_no": ref.subject_no, "profile_fingerprint": profile_fingerprint,
             "changed_fields": list(changed_fields)},
            self._at(at),
        )
        return subject_ref

    def verify_subject(
        self, subject_ref: str, *, reviewer: str, result: str,
        at: Optional[str | datetime] = None,
    ) -> str:
        if result not in ("CONFIRMED", "REJECTED"):
            raise ValueError("核验结论只能是 CONFIRMED 或 REJECTED")
        self._require_agency(reviewer)
        subject = self._require_subject(subject_ref)
        _, agg_id = subject_ref.split(":", 1)
        ts = self._at(at)
        self._emit("SUBJECT_VERIFIED", "regulated_subject", agg_id,
                   {"reviewer": reviewer, "result": result}, ts)
        self._log_decision(reviewer, subject_ref, f"SUBJECT_VERIFIED:{result}",
                           f"主体 {subject.name} 隔离核验", ts)
        return subject_ref

    def register_node(
        self, node_type: str, node_id: str, at: Optional[str | datetime] = None,
        event_id: Optional[str] = None, **attrs: Any,
    ) -> str:
        ref = aref(node_type, node_id)
        _, agg_id = ref.split(":", 1)
        payload: dict[str, Any] = {"node_type": node_type, "node_ref": ref}
        payload.update(attrs)
        self._emit("NODE_REGISTERED", node_type, agg_id, payload,
                   self._at(at), event_id)
        return ref

    def link(
        self, link_type: str, from_ref: str, to_ref: str, *,
        clue_ref: Optional[str] = None, valid_from: Optional[str | datetime] = None,
        valid_until: Optional[str | datetime] = None,
        at: Optional[str | datetime] = None,
    ) -> None:
        for endpoint in (from_ref, to_ref):
            if endpoint not in self.state.nodes and endpoint not in self.state.subjects:
                raise UnknownReference(f"关系端点不存在: {endpoint}")
            self._require_not_quarantined(endpoint)
        ts = self._at(at)
        vf = self._at(valid_from) if valid_from is not None else ts
        vu = self._at(valid_until) if valid_until is not None else None
        # 同类型、同端点、同生效区间的边重复建立视为重放，保持幂等。
        for existing in self.state.links:
            if (existing.link_type, existing.from_ref, existing.to_ref,
                    existing.valid_until) == (link_type, from_ref, to_ref, vu) \
                    and existing.valid_from == vf:
                return
        payload: dict[str, Any] = {
            "link_type": link_type, "from_ref": from_ref, "to_ref": to_ref,
            "valid_from": vf.isoformat(),
        }
        if vu is not None:
            payload["valid_until"] = vu.isoformat()
        if clue_ref:
            payload["clue_ref"] = clue_ref
        self._emit("LINK_ESTABLISHED", "risk_clue", _edge_id(from_ref, link_type, to_ref),
                   payload, ts)

    # -- 职责与线索 --------------------------------------------------------

    def declare_duty(self, agency_id: str, duty_code: str,
                     at: Optional[str | datetime] = None) -> None:
        self._emit("DUTY_DECLARED", "legal_duty", f"{agency_id}:{duty_code}",
                   {"agency_id": agency_id, "duty_code": duty_code}, self._at(at))

    def register_clue(
        self, clue_id: str, *, source_type: str, subject_ref: str,
        reporter: Optional[str] = None, reporter_contact: Optional[str] = None,
        case_detail: str = "", at: Optional[str | datetime] = None,
        event_id: Optional[str] = None,
    ) -> str:
        self._require_subject(subject_ref)
        payload = {"source_type": source_type, "subject_ref": subject_ref,
                   "case_detail": case_detail}
        if reporter is not None:
            payload["reporter"] = reporter
        if reporter_contact is not None:
            payload["reporter_contact"] = reporter_contact
        self._emit("CLUE_REGISTERED", "risk_clue", clue_id, payload,
                   self._at(at), event_id)
        return aref("risk_clue", clue_id)

    # -- 证据：提交、终审、更正 ---------------------------------------------

    def submit_evidence(
        self, evidence_id: str, *, evidence_kind: str, submitted_by: str,
        clue_ref: Optional[str] = None, at: Optional[str | datetime] = None,
        event_id: Optional[str] = None,
    ) -> str:
        ref = aref("evidence_record", evidence_id)
        payload = {"evidence_ref": ref, "submitted_by": submitted_by,
                   "evidence_kind": evidence_kind}
        if clue_ref:
            payload["clue_ref"] = clue_ref
        self._emit("EVIDENCE_SUBMITTED", "evidence_record", evidence_id, payload,
                   self._at(at), event_id)
        return ref

    def review_evidence(
        self, evidence_ref: str, *, reviewer: str, verdict: str,
        reject_reason: Optional[str] = None, at: Optional[str | datetime] = None,
    ) -> None:
        if verdict not in (graph.EV_APPROVED, graph.EV_REJECTED):
            raise ValueError("终审结论只能是 APPROVED 或 REJECTED")
        self._require_agency(reviewer)
        ev = self.state.evidence.get(evidence_ref)
        if ev is None:
            raise UnknownReference(f"证据不存在: {evidence_ref}")
        if ev.status != graph.EV_PENDING:
            raise ReviewerConflict(f"证据 {evidence_ref} 当前状态 {ev.status}，不能再审")
        if ev.submitted_by == reviewer:
            raise ReviewerConflict("提交人不得为自己提交的证据作终审")
        _, agg_id = evidence_ref.split(":", 1)
        payload = {"evidence_ref": evidence_ref, "reviewer": reviewer, "verdict": verdict}
        if reject_reason:
            payload["reject_reason"] = reject_reason
        ts = self._at(at)
        self._emit("EVIDENCE_REVIEWED", "evidence_record", agg_id, payload, ts)
        self._log_decision(reviewer, evidence_ref, f"EVIDENCE_{verdict}",
                           f"对 {ev.submitted_by} 提交的 {ev.kind} 终审", ts)

    def correct_evidence(
        self, corrected_ref: str, *, new_evidence_id: str, submitted_by: str,
        correction_reason: str, at: Optional[str | datetime] = None,
    ) -> str:
        """检测结论更正：旧证据失效，只重开真正依赖它的处置与案件。"""
        old = self.state.evidence.get(corrected_ref)
        if old is None:
            raise UnknownReference(f"被更正证据不存在: {corrected_ref}")
        new_ref = aref("evidence_record", new_evidence_id)
        _, agg_id = corrected_ref.split(":", 1)
        self._emit(
            "EVIDENCE_CORRECTED", "evidence_record", agg_id,
            {"corrected_ref": corrected_ref, "correction_reason": correction_reason,
             "new_evidence_ref": new_ref, "submitted_by": submitted_by,
             "clue_ref": old.clue_ref},
            self._at(at),
        )
        return new_ref

    # -- 移送：创建、接收、退回 ---------------------------------------------

    def create_handoff(
        self, handoff_id: str, *, from_agency: str, to_agency: str,
        due_at: str | datetime, clue_ref: str, duty_code: Optional[str] = None,
        at: Optional[str | datetime] = None,
    ) -> str:
        self._require_agency(from_agency)
        if to_agency not in self.state.duties:
            raise OutOfJurisdiction(f"接收部门 {to_agency} 尚未声明法定职责")
        if duty_code is not None and duty_code not in self.state.duties[to_agency]:
            raise OutOfJurisdiction(
                f"{to_agency} 没有职责 {duty_code}，不能向其移送 {clue_ref}"
            )
        ref = aref("agency_handoff", handoff_id)
        self._emit(
            "HANDOFF_CREATED", "agency_handoff", handoff_id,
            {"from_agency": from_agency, "to_agency": to_agency,
             "due_at": self._at(due_at).isoformat(), "clue_ref": clue_ref},
            self._at(at),
        )
        return ref

    def _get_pending_handoff(self, handoff_ref: str, agency_id: str) -> graph.HandoffState:
        h = self.state.handoffs.get(handoff_ref)
        if h is None:
            raise UnknownReference(f"移送不存在: {handoff_ref}")
        if h.to_agency != agency_id:
            raise OutOfJurisdiction(f"移送 {handoff_ref} 的接收方不是 {agency_id}")
        if h.status != graph.H_PENDING:
            raise HandoffStateError(f"移送 {handoff_ref} 当前状态 {h.status}，不能重复处置")
        return h

    def accept_handoff(
        self, handoff_ref: str, *, agency_id: str, handled_due_at: str | datetime,
        at: Optional[str | datetime] = None,
    ) -> None:
        h = self._get_pending_handoff(handoff_ref, agency_id)
        _, agg_id = handoff_ref.split(":", 1)
        ts = self._at(at)
        self._emit("HANDOFF_ACCEPTED", "agency_handoff", agg_id,
                   {"agency_id": agency_id, "due_at": self._at(handled_due_at).isoformat()}, ts)
        self._log_decision(agency_id, handoff_ref, "HANDOFF_ACCEPTED",
                           f"接收 {h.from_agency} 移送的线索 {h.clue_ref}", ts)

    def return_handoff(
        self, handoff_ref: str, *, agency_id: str, return_reason: str,
        at: Optional[str | datetime] = None,
    ) -> None:
        h = self._get_pending_handoff(handoff_ref, agency_id)
        _, agg_id = handoff_ref.split(":", 1)
        ts = self._at(at)
        self._emit("HANDOFF_RETURNED", "agency_handoff", agg_id,
                   {"agency_id": agency_id, "return_reason": return_reason}, ts)
        self._log_decision(agency_id, handoff_ref, "HANDOFF_RETURNED",
                           f"退回 {h.from_agency} 的移送：{return_reason}", ts)

    # -- 范围限制：紧急下架/封存、解除 ---------------------------------------

    def restrict_scope(
        self, scope_type: str, scope_ref: str, *, agency_id: str, reason: str,
        emergency: bool, clue_ref: Optional[str] = None,
        at: Optional[str | datetime] = None, event_id: Optional[str] = None,
    ) -> None:
        if scope_type not in ("LISTING", "BATCH", "PREMISE", "SUBJECT"):
            raise ValueError("scope_type 必须是 LISTING/BATCH/PREMISE/SUBJECT")
        self._require_agency(agency_id)
        if scope_ref not in self.state.nodes and scope_ref not in self.state.subjects:
            raise UnknownReference(f"限制对象不存在: {scope_ref}")
        payload = {"scope_type": scope_type, "scope_ref": scope_ref,
                   "agency_id": agency_id, "reason": reason, "emergency": emergency}
        if clue_ref:
            payload["clue_ref"] = clue_ref
        self._emit("SCOPE_RESTRICTED", "enforcement_action",
                   f"restrict:{scope_type}:{scope_ref.split(':', 1)[-1]}",
                   payload, self._at(at), event_id)

    def release_scope(
        self, scope_type: str, scope_ref: str, *, reason: str,
        at: Optional[str | datetime] = None,
    ) -> None:
        """解除限制必须有覆盖该范围的法定处置结论，或关联整改已复查通过。"""
        key = (scope_type, scope_ref)
        restriction = self.state.restrictions.get(key)
        if restriction is None:
            raise UnknownReference(f"该范围从未被限制: {key}")
        if restriction.released:
            raise HandoffStateError(f"限制 {key} 已经解除")
        legal_done = any(
            a.status == graph.A_RESOLVED and a.scope == key
            for a in self.state.enforcements.values()
        )
        rect_done = restriction.clue_ref is not None and any(
            rc.clue_ref == restriction.clue_ref and rc.status == graph.R_PASSED
            for rc in self.state.rectifications.values()
        )
        if not (legal_done or rect_done):
            raise HandoffStateError(
                "紧急下架/封存只阻止继续流通；须先完成覆盖该范围的法定处置或复查通过，方可解除"
            )
        self._emit("SCOPE_RELEASED", "enforcement_action",
                   f"release:{scope_type}:{scope_ref.split(':', 1)[-1]}",
                   {"scope_type": scope_type, "scope_ref": scope_ref, "reason": reason},
                   self._at(at))

    # -- 法定处置 -----------------------------------------------------------

    def open_enforcement(
        self, action_id: str, *, agency_id: str, basis_refs: list[str],
        clue_ref: str, scope: Optional[tuple[str, str]] = None,
        duty_code: Optional[str] = None, at: Optional[str | datetime] = None,
    ) -> str:
        self._assert_may_handle(agency_id, clue_ref)
        if duty_code is not None and duty_code not in self.state.duties[agency_id]:
            raise OutOfJurisdiction(f"{agency_id} 没有职责 {duty_code}")
        missing = [r for r in basis_refs if r not in self.state.evidence]
        if missing:
            raise UnknownReference(f"作为处置依据的证据不存在: {missing}")
        not_approved = [
            r for r in basis_refs if self.state.evidence[r].status != graph.EV_APPROVED
        ]
        if not_approved:
            raise ReviewerConflict(f"法定处置只能依据终审通过的证据，以下证据不可用: {not_approved}")
        payload = {"agency_id": agency_id, "basis_refs": list(basis_refs),
                   "clue_ref": clue_ref}
        if scope is not None:
            payload["scope_type"], payload["scope_ref"] = scope
        ref = aref("enforcement_action", action_id)
        self._emit("ENFORCEMENT_OPENED", "enforcement_action", action_id, payload,
                   self._at(at))
        return ref

    def resolve_enforcement(
        self, action_ref: str, *, agency_id: str, resolution: str,
        at: Optional[str | datetime] = None,
    ) -> None:
        action = self.state.enforcements.get(action_ref)
        if action is None:
            raise UnknownReference(f"处置不存在: {action_ref}")
        if action.agency_id != agency_id:
            raise OutOfJurisdiction(f"处置 {action_ref} 由 {action.agency_id} 管辖")
        if action.status != graph.A_OPEN:
            raise HandoffStateError(f"处置 {action_ref} 当前不是办理中状态")
        _, agg_id = action_ref.split(":", 1)
        ts = self._at(at)
        self._emit("ENFORCEMENT_RESOLVED", "enforcement_action", agg_id,
                   {"agency_id": agency_id, "resolution": resolution}, ts)
        self._log_decision(agency_id, action_ref, "ENFORCEMENT_RESOLVED", resolution, ts)

    # -- 整改与复查 ----------------------------------------------------------

    def request_rectification(
        self, rect_id: str, *, agency_id: str, clue_ref: str,
        due_at: str | datetime, at: Optional[str | datetime] = None,
    ) -> str:
        self._assert_may_handle(agency_id, clue_ref)
        ref = aref("rectification", rect_id)
        self._emit("RECTIFICATION_REQUESTED", "rectification", rect_id,
                   {"agency_id": agency_id, "due_at": self._at(due_at).isoformat(),
                    "clue_ref": clue_ref},
                   self._at(at))
        return ref

    def submit_rectification(
        self, rect_ref: str, *, evidence_ref: str, submitter: str,
        at: Optional[str | datetime] = None,
    ) -> None:
        rc = self.state.rectifications.get(rect_ref)
        if rc is None:
            raise UnknownReference(f"整改不存在: {rect_ref}")
        if rc.status not in (graph.R_REQUESTED, graph.R_REJECTED):
            raise HandoffStateError(f"整改 {rect_ref} 当前状态 {rc.status}，不能提交")
        ev = self.state.evidence.get(evidence_ref)
        if ev is None:
            raise UnknownReference(f"整改材料证据不存在: {evidence_ref}")
        if ev.submitted_by != submitter:
            raise ReviewerConflict("整改材料须由提交人本人提交")
        _, agg_id = rect_ref.split(":", 1)
        self._emit("RECTIFICATION_SUBMITTED", "rectification", agg_id,
                   {"evidence_ref": evidence_ref, "submitter": submitter},
                   self._at(at))

    def _review_rectification(
        self, rect_ref: str, reviewer: str, at: datetime,
    ) -> graph.RectificationState:
        rc = self.state.rectifications.get(rect_ref)
        if rc is None:
            raise UnknownReference(f"整改不存在: {rect_ref}")
        self._require_agency(reviewer)
        if rc.status != graph.R_SUBMITTED:
            raise HandoffStateError(f"整改 {rect_ref} 尚未提交复查，当前 {rc.status}")
        if rc.submitter == reviewer:
            raise ReviewerConflict("整改提交人不得复查自己的整改材料")
        return rc

    def pass_rectification(
        self, rect_ref: str, *, reviewer: str, at: Optional[str | datetime] = None,
    ) -> None:
        ts = self._at(at)
        rc = self._review_rectification(rect_ref, reviewer, ts)
        ev = self.state.evidence[rc.evidence_ref]
        if ev.status != graph.EV_APPROVED:
            raise ReviewerConflict(
                f"整改材料 {rc.evidence_ref} 尚未终审通过，不能作出复查通过结论"
            )
        _, agg_id = rect_ref.split(":", 1)
        self._emit("RECTIFICATION_PASSED", "rectification", agg_id,
                   {"reviewer": reviewer, "evidence_ref": rc.evidence_ref}, ts)
        self._log_decision(reviewer, rect_ref, "RECTIFICATION_PASSED",
                           f"复查通过，材料 {rc.evidence_ref}", ts)

    def reject_rectification(
        self, rect_ref: str, *, reviewer: str, reject_reason: str,
        at: Optional[str | datetime] = None,
    ) -> None:
        ts = self._at(at)
        rc = self._review_rectification(rect_ref, reviewer, ts)
        _, agg_id = rect_ref.split(":", 1)
        self._emit("RECTIFICATION_REJECTED", "rectification", agg_id,
                   {"reviewer": reviewer, "reject_reason": reject_reason}, ts)
        self._log_decision(reviewer, rect_ref, "RECTIFICATION_REJECTED", reject_reason, ts)

    # -- 结案 ---------------------------------------------------------------

    def close_case(self, clue_ref: str, *, closed_by: str,
                   at: Optional[str | datetime] = None) -> None:
        self._require_agency(closed_by)
        ts = self._at(at)
        blockers = closure_blockers(self.state, clue_ref, ts)
        if blockers:
            raise ClosureBlocked(blockers)
        _, agg_id = clue_ref.split(":", 1)
        self._emit("CASE_CLOSED", "risk_clue", agg_id, {"closed_by": closed_by}, ts)
        self._log_decision(closed_by, clue_ref, "CASE_CLOSED", "全链闭合，准予结案", ts)

    # -- 外部回调 -----------------------------------------------------------

    def ingest_callback(self, event: Mapping[str, Any]) -> bool:
        """接收平台/外部系统回调事件。

        同一 event_id 重放返回 False（幂等）；同号内容变化由存储层抛
        DuplicateConflict，调用方必须走主体档案隔离核验而不是覆盖。
        """
        issues = validate_event(event, self.schema)
        if issues:
            raise ContractViolation([f"{i.field} {i.code} {i.message}" for i in issues])
        # 已存在的 event_id 属于重放，存储层负责同号内容一致性比对，不再做版本检查。
        if not self.store.has_event(event["event_id"]):
            key = (event["aggregate_type"], event["aggregate_id"])
            expected = self.state.versions.get(key, 0) + 1
            if event["version"] != expected:
                from .errors import VersionConflict

                raise VersionConflict(
                    f"聚合 {key} 下一版本应为 {expected}，回调声明 {event['version']}"
                )
        appended = self.store.append(event)
        if appended:
            graph.apply_event(self.state, event)
        return appended


def _edge_id(from_ref: str, link_type: str, to_ref: str) -> str:
    return f"{from_ref}--{link_type}--{to_ref}".replace(":", "_")
