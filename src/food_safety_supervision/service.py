"""食品安全协同督办领域服务。

所有状态都由事件流折叠得到：服务重建即可从故障中恢复，命令推进版本链，
审计日志记录每个决定。图节点（主体、场所、页面、批次、样本、线索、移送、
处置、整改）之间的关系带生效时间，查询给出卡点部门、受限范围与闭合缺口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from .audit import AuditLog
from .contracts import validate_event
from .errors import (
    ContractViolation,
    HandoffClosed,
    IllegalState,
    JurisdictionError,
    ReviewerConflict,
)
from .events import EventStore

POSITIVE_MARKERS = ("positive", "non_compliant", "不合格", "阳性", "超标")
QUALIFIED_MARKERS = ("qualified", "合格", "阴性")


def _is_positive(conclusion: str) -> bool:
    return any(marker in conclusion for marker in POSITIVE_MARKERS)


def _is_qualified(conclusion: str) -> bool:
    return not _is_positive(conclusion) and any(
        marker in conclusion for marker in QUALIFIED_MARKERS
    )

# 默认部门职责：事实类型 -> 唯一有权确认的部门。
DEFAULT_MANDATES: dict[str, str] = {
    "premise_address": "MARKET_REGULATION",
    "license_status": "MARKET_REGULATION",
    "listing_identity_verified": "MARKET_REGULATION",
    "subject_status": "MARKET_REGULATION",
    "rectification_review": "MARKET_REGULATION",
    "batch_source": "AGRICULTURE",
    "transport_condition": "AGRICULTURE",
    "test_conclusion": "INSPECTION",
    "investigation_clue": "PUBLIC_SECURITY",
}

# 各类节点的归口部门，用于回答“当前卡在哪个部门”。
ENTITY_OWNER = {
    "licensed_premise": "MARKET_REGULATION",
    "platform_listing": "MARKET_REGULATION",
    "material_batch": "AGRICULTURE",
    "test_sample": "INSPECTION",
}


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass
class Subject:
    subject_no: str
    name: str
    aliases: set[str] = field(default_factory=set)


@dataclass
class Premise:
    premise_id: str
    subject_no: str
    address: str
    license_no: str
    valid_from: str
    valid_to: str | None = None
    quarantined: bool = True
    verified: bool = False


@dataclass
class Listing:
    listing_id: str
    platform: str
    subject_no: str
    url: str
    fingerprint: str
    observed_at: str
    quarantined: bool = True
    verified: bool = False
    fingerprint_changed: bool = False


@dataclass
class Batch:
    batch_id: str
    description: str
    links: list[str] = field(default_factory=list)


@dataclass
class Sample:
    sample_id: str
    batch_id: str
    conclusion: str | None = None
    history: list[dict[str, str]] = field(default_factory=list)


@dataclass
class Clue:
    clue_id: str
    source_type: str
    subject_ref: str
    refs: list[str] = field(default_factory=list)
    reporter: dict[str, Any] = field(default_factory=dict)
    closed_at: str | None = None


@dataclass
class Handoff:
    handoff_id: str
    from_agency: str
    to_agency: str
    due_at: str
    refs: list[str]
    status: str = "opened"  # opened / accepted / rejected
    reason: str | None = None
    decided_at: str | None = None


@dataclass
class Fact:
    fact_id: str
    clue_id: str
    agency_id: str
    fact_type: str
    refs: list[str]
    result: str


@dataclass
class Evidence:
    evidence_id: str
    submitted_by: str
    refs: list[str]
    reviewer: str | None = None
    decision: str | None = None
    reviewed_at: str | None = None


@dataclass
class Action:
    action_id: str
    agency_id: str
    scope_type: str
    scope_ref: str
    kind: str  # emergency / statutory
    action_type: str
    legal_basis: str | None
    based_on: list[str]
    refs: list[str]
    at: str
    status: str = "active"  # active / lifted / reopened
    reopen_reasons: list[str] = field(default_factory=list)
    statutory_successor: str | None = None
    rectification_required: bool = True
    restrictive: bool = True  # 是否直接限制商品/店铺继续流通


@dataclass
class Rectification:
    rectification_id: str
    refs: list[str]
    submitted_by: str
    submitted_at: str
    review_due_at: str | None
    reviewer: str | None = None
    passed: bool | None = None
    reviewed_at: str | None = None


class SupervisionService:
    def __init__(
        self,
        store: EventStore,
        audit: AuditLog | None = None,
        schema: Mapping[str, Any] | None = None,
        mandates: Mapping[str, str] | None = None,
    ) -> None:
        self.store = store
        self.audit = audit or AuditLog()
        if schema is None:
            import json
            from pathlib import Path

            schema_path = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.schema = schema
        self.mandates = dict(mandates or DEFAULT_MANDATES)

        self.subjects: dict[str, Subject] = {}
        self.premises: dict[str, Premise] = {}
        self.listing_by_id: dict[str, Listing] = {}
        self.batches: dict[str, Batch] = {}
        self.samples: dict[str, Sample] = {}
        self.clues: dict[str, Clue] = {}
        self.handoffs: dict[str, Handoff] = {}
        self.facts: dict[str, Fact] = {}
        self.evidence: dict[str, Evidence] = {}
        self.callbacks: dict[tuple[str, str], str] = {}
        self.actions: dict[str, Action] = {}
        self.rectifications: dict[str, Rectification] = {}
        self.overdue: dict[tuple[str, str], dict[str, Any]] = {}
        self._sample_dependents: dict[str, set[str]] = {}
        self._rebuild()

    # ------------------------------------------------------------------ 折叠

    def _rebuild(self) -> None:
        for event in self.store.all_events():
            self._apply(event, audit=False)

    def _apply(self, event: Mapping[str, Any], audit: bool = True) -> None:
        etype = event["event_type"]
        p = event["payload"]
        at = event["occurred_at"]
        handler = getattr(self, f"_apply_{etype.lower()}", None)
        if handler is not None:
            handler(event["aggregate_id"], p, at)

    def _apply_subject_registered(self, agg_id: str, p: dict[str, Any], at: str) -> None:
        subject = self.subjects.setdefault(p["subject_no"], Subject(p["subject_no"], p["name"]))
        subject.name = p["name"]
        for alias in p.get("aliases", []):
            subject.aliases.add(alias)

    def _apply_premise_linked(self, agg_id: str, p: dict[str, Any], at: str) -> None:
        if agg_id in self.premises:
            return
        current = [
            pr for pr in self.premises.values()
            if pr.subject_no == p["subject_no"] and pr.valid_to is None
        ]
        # 首版许可场所不隔离（仍需现场检查确认）；只有主体号相同但地址/许可证
        # 发生变化时，旧版本到期、新版本隔离核验。
        quarantined = False
        for previous in current:
            if previous.address != p["address"] or previous.license_no != p["license_no"]:
                previous.valid_to = p["valid_from"]
                quarantined = True
        self.premises[agg_id] = Premise(
            agg_id, p["subject_no"], p["address"], p["license_no"], p["valid_from"],
            quarantined=quarantined, verified=p.get("verified", False),
        )

    def _apply_listing_linked(self, agg_id: str, p: dict[str, Any], at: str) -> None:
        self.listing_by_id[agg_id] = Listing(
            agg_id, p["platform"], p["subject_no"], p["url"],
            p["content_fingerprint"], p["observed_at"],
            quarantined=not p.get("verified", False), verified=p.get("verified", False),
        )

    def _apply_batch_linked(self, agg_id: str, p: dict[str, Any], at: str) -> None:
        self.batches[agg_id] = Batch(agg_id, p["description"], list(p.get("refs", [])))

    def _apply_sample_linked(self, agg_id: str, p: dict[str, Any], at: str) -> None:
        self.samples[agg_id] = Sample(agg_id, p["batch_id"])

    def _apply_clue_registered(self, agg_id: str, p: dict[str, Any], at: str) -> None:
        self.clues[agg_id] = Clue(
            agg_id, p["source_type"], p["subject_ref"],
            list(p.get("refs", [])),
            {k: v for k, v in p.items() if k.startswith("reporter")},
        )

    def _apply_handoff_opened(self, agg_id: str, p: dict[str, Any], at: str) -> None:
        self.handoffs[agg_id] = Handoff(
            agg_id, p["from_agency"], p["to_agency"], p["due_at"], list(p["refs"])
        )

    def _apply_handoff_accepted(self, agg_id: str, p: dict[str, Any], at: str) -> None:
        handoff = self.handoffs[agg_id]
        handoff.status = "accepted"
        handoff.decided_at = at
        handoff.due_at = p.get("due_at", handoff.due_at)
        overdue = self.overdue.get(("agency_handoff", agg_id))
        if overdue:
            overdue["resolved_at"] = at

    def _apply_handoff_rejected(self, agg_id: str, p: dict[str, Any], at: str) -> None:
        handoff = self.handoffs[agg_id]
        handoff.status = "rejected"
        handoff.reason = p["reason"]
        handoff.decided_at = at
        overdue = self.overdue.get(("agency_handoff", agg_id))
        if overdue:
            overdue["resolved_at"] = at

    def _apply_fact_confirmed(self, agg_id: str, p: dict[str, Any], at: str) -> None:
        fact_id = p.get("fact_id", f"{agg_id}:{p['fact_type']}:{len(p['refs'])}")
        fact = Fact(fact_id, agg_id, p["agency_id"], p["fact_type"], list(p["refs"]),
                    p.get("result", "confirmed"))
        self.facts[fact_id] = fact
        if p["fact_type"] in ("listing_identity_verified", "premise_address", "license_status"):
            verified_ok = p.get("result", "confirmed") != "violation"
            for ref in p["refs"]:
                if ref in self.listing_by_id and p["fact_type"] == "listing_identity_verified":
                    self.listing_by_id[ref].quarantined = not verified_ok
                    self.listing_by_id[ref].verified = verified_ok
                if ref in self.premises and p["fact_type"] in ("premise_address", "license_status"):
                    self.premises[ref].quarantined = not verified_ok
                    self.premises[ref].verified = verified_ok

    def _apply_evidence_submitted(self, agg_id: str, p: dict[str, Any], at: str) -> None:
        evidence = self.evidence.get(p.get("evidence_id", agg_id))
        if evidence is not None and evidence.reviewer is None:
            return
        self.evidence[p.get("evidence_id", agg_id)] = Evidence(
            p.get("evidence_id", agg_id), p["submitted_by"], list(p["refs"])
        )

    def _apply_evidence_finalized(self, agg_id: str, p: dict[str, Any], at: str) -> None:
        evidence = self.evidence[p["evidence_id"]]
        evidence.reviewer = p["reviewer"]
        evidence.decision = p["decision"]
        evidence.reviewed_at = at

    def _apply_callback_recorded(self, agg_id: str, p: dict[str, Any], at: str) -> None:
        key = (p["platform"], p["callback_id"])
        self.callbacks.setdefault(key, agg_id)
        existing = self.listing_by_id.get(agg_id)
        if existing is None:
            self.listing_by_id[agg_id] = Listing(
                agg_id, p["platform"], p["subject_no"], p.get("url", ""),
                p["content_fingerprint"], p["observed_at"], quarantined=True,
            )
            return
        # 主体号相同但内容指纹变化：旧版本保留，新快照隔离核验。
        if existing.fingerprint != p["content_fingerprint"]:
            existing.fingerprint = p["content_fingerprint"]
            existing.observed_at = p["observed_at"]
            existing.quarantined = True
            existing.verified = False
            existing.fingerprint_changed = True

    def _apply_scope_restricted(self, agg_id: str, p: dict[str, Any], at: str) -> None:
        action = Action(
            agg_id, p.get("agency_id", ""), p["scope_type"], p["scope_ref"],
            "emergency" if p.get("emergency", True) else "statutory",
            p.get("action_type", "紧急下架/封存"), p.get("legal_basis"),
            list(p.get("based_on", [])), list(p.get("refs", [])), at,
            restrictive=p.get("restrictive", True),
        )
        self.actions[agg_id] = action
        for sample_id in action.based_on:
            self._sample_dependents.setdefault(sample_id, set()).add(agg_id)

    def _apply_statutory_action_taken(self, agg_id: str, p: dict[str, Any], at: str) -> None:
        action = Action(
            agg_id, p.get("agency_id", ""), p.get("scope_type", "chain"),
            p.get("scope_ref", ""), "statutory", p["action_type"],
            p["legal_basis"], list(p.get("based_on", [])), list(p.get("refs", [])), at,
            rectification_required=p.get("rectification_required", True),
            restrictive=p.get("restrictive", False),
        )
        self.actions[agg_id] = action
        for sample_id in action.based_on:
            self._sample_dependents.setdefault(sample_id, set()).add(agg_id)
        for ref in p.get("refs", []):
            emergency = self.actions.get(ref)
            if emergency is not None and emergency.kind == "emergency":
                emergency.statutory_successor = agg_id
        for ratified_id in p.get("ratifies", []):
            emergency = self.actions.get(ratified_id)
            if emergency is not None and emergency.kind == "emergency":
                # 法定程序追认：紧急措施继续生效但已有法定依据。
                emergency.statutory_successor = agg_id
        for lifted_id in p.get("lifts", []):
            if lifted_id in self.actions:
                self.actions[lifted_id].status = "lifted"

    def _apply_test_concluded(self, agg_id: str, p: dict[str, Any], at: str) -> None:
        sample = self.samples[agg_id]
        sample.conclusion = p["conclusion"]
        sample.history.append({"conclusion": p["conclusion"], "at": at, "by": "conclusion"})

    def _apply_test_corrected(self, agg_id: str, p: dict[str, Any], at: str) -> None:
        sample = self.samples[agg_id]
        sample.conclusion = p["new_conclusion"]
        sample.history.append({"conclusion": p["new_conclusion"], "at": at, "by": "correction"})

    def _apply_action_reopened(self, agg_id: str, p: dict[str, Any], at: str) -> None:
        action = self.actions[agg_id]
        action.status = "reopened"
        action.reopen_reasons.append(p["reason"])

    def _apply_rectification_submitted(self, agg_id: str, p: dict[str, Any], at: str) -> None:
        self.rectifications[agg_id] = Rectification(
            agg_id, list(p.get("refs", [])), p["submitted_by"], at, p.get("review_due_at")
        )

    def _apply_rectification_reviewed(self, agg_id: str, p: dict[str, Any], at: str) -> None:
        rect = self.rectifications[agg_id]
        rect.reviewer = p["reviewer"]
        rect.passed = p["passed"]
        rect.reviewed_at = at
        overdue = self.overdue.get(("rectification", agg_id))
        if overdue:
            overdue["resolved_at"] = at

    def _apply_overdue_flagged(self, agg_id: str, p: dict[str, Any], at: str) -> None:
        key = (p["ref_type"], p["ref_id"])
        self.overdue.setdefault(key, {"flagged_at": at, "resolved_at": None,
                                      "rectification": None})

    def _apply_case_closed(self, agg_id: str, p: dict[str, Any], at: str) -> None:
        self.clues[agg_id].closed_at = at

    # ------------------------------------------------------------------ 发射

    def _emit(self, event: dict[str, Any], *, actor: str, decision: str,
              target: str | None = None, audit_detail: dict[str, Any] | None = None) -> dict[str, Any]:
        issues = validate_event(event, self.schema)
        if issues:
            raise ContractViolation([f"{i.field}: {i.message}" for i in issues])
        is_replay = self.store.has_event(event["event_id"])
        stored = self.store.append(event)
        if is_replay:
            # 同标识同内容的重放：状态与审计都保持不变。
            return stored
        self._apply(stored)
        self.audit.decision(actor, decision, target or event["aggregate_id"],
                            event_type=event["event_type"], **(audit_detail or {}))
        return stored

    def _version(self, aggregate_type: str, aggregate_id: str) -> int:
        return self.store.version(aggregate_type, aggregate_id) + 1

    def _require_mandate(self, agency_id: str, fact_type: str) -> None:
        if self.mandates.get(fact_type) != agency_id:
            raise JurisdictionError(
                f"部门 {agency_id} 无权确认事实 {fact_type}；"
                f"职责归属 {self.mandates.get(fact_type)}"
            )

    def _resolve_subject(self, ref: str) -> str:
        if ref in self.subjects:
            return ref
        for subject in self.subjects.values():
            if ref in subject.aliases:
                return subject.subject_no
        raise IllegalState(f"未知经营主体编号: {ref}")

    # ------------------------------------------------------------------ 命令

    def register_subject(self, event_id: str, subject_no: str, name: str, at: str,
                         aliases: tuple[str, ...] = (), actor: str | None = None) -> dict[str, Any] | None:
        merged_aliases = set(aliases)
        if subject_no in self.subjects:
            merged_aliases |= self.subjects[subject_no].aliases
        event = {
            "event_id": event_id,
            "event_type": "SUBJECT_REGISTERED",
            "aggregate_type": "regulated_subject",
            "aggregate_id": subject_no,
            "occurred_at": at,
            "version": self._version("regulated_subject", subject_no),
            "payload": {"subject_no": subject_no, "name": name, "aliases": sorted(merged_aliases)},
        }
        stored = self._emit(event, actor=actor or "system", decision="register_subject")
        if self.store.has_event(event_id):
            return None
        return stored

    def link_premise(self, event_id: str, premise_id: str, subject_no: str, address: str,
                     license_no: str, valid_from: str, at: str, verified: bool = False,
                     actor: str | None = None) -> dict[str, Any]:
        subject_no = self._resolve_subject(subject_no)
        event = {
            "event_id": event_id,
            "event_type": "PREMISE_LINKED",
            "aggregate_type": "licensed_premise",
            "aggregate_id": premise_id,
            "occurred_at": at,
            "version": self._version("licensed_premise", premise_id),
            "payload": {
                "premise_id": premise_id, "subject_no": subject_no, "address": address,
                "license_no": license_no, "valid_from": valid_from, "verified": verified,
            },
        }
        return self._emit(event, actor=actor or "system", decision="link_premise",
                          audit_detail={"quarantine": not verified})

    def link_listing(self, event_id: str, listing_id: str, platform: str, subject_no: str,
                     url: str, fingerprint: str, observed_at: str, at: str,
                     verified: bool = False, actor: str | None = None) -> dict[str, Any]:
        subject_no = self._resolve_subject(subject_no)
        event = {
            "event_id": event_id,
            "event_type": "LISTING_LINKED",
            "aggregate_type": "platform_listing",
            "aggregate_id": listing_id,
            "occurred_at": at,
            "version": self._version("platform_listing", listing_id),
            "payload": {
                "listing_id": listing_id, "platform": platform, "subject_no": subject_no,
                "url": url, "content_fingerprint": fingerprint, "observed_at": observed_at,
                "verified": verified,
            },
        }
        return self._emit(event, actor=actor or "system", decision="link_listing")

    def link_batch(self, event_id: str, batch_id: str, description: str, at: str,
                   refs: tuple[str, ...] = (), actor: str | None = None) -> dict[str, Any]:
        event = {
            "event_id": event_id,
            "event_type": "BATCH_LINKED",
            "aggregate_type": "material_batch",
            "aggregate_id": batch_id,
            "occurred_at": at,
            "version": self._version("material_batch", batch_id),
            "payload": {"batch_id": batch_id, "description": description, "refs": list(refs)},
        }
        return self._emit(event, actor=actor or "system", decision="link_batch")

    def link_sample(self, event_id: str, sample_id: str, batch_id: str, at: str,
                    actor: str | None = None) -> dict[str, Any]:
        event = {
            "event_id": event_id,
            "event_type": "SAMPLE_LINKED",
            "aggregate_type": "test_sample",
            "aggregate_id": sample_id,
            "occurred_at": at,
            "version": self._version("test_sample", sample_id),
            "payload": {"sample_id": sample_id, "batch_id": batch_id},
        }
        return self._emit(event, actor=actor or "system", decision="link_sample")

    def register_clue(self, event_id: str, clue_id: str, source_type: str, subject_ref: str,
                      at: str, refs: tuple[str, ...] = (), reporter_name: str | None = None,
                      reporter_contact: str | None = None, actor: str | None = None) -> dict[str, Any]:
        subject_ref = self._resolve_subject(subject_ref)
        payload: dict[str, Any] = {
            "source_type": source_type, "subject_ref": subject_ref, "refs": list(refs),
        }
        if reporter_name is not None:
            payload["reporter_name"] = reporter_name
        if reporter_contact is not None:
            payload["reporter_contact"] = reporter_contact
        event = {
            "event_id": event_id,
            "event_type": "CLUE_REGISTERED",
            "aggregate_type": "risk_clue",
            "aggregate_id": clue_id,
            "occurred_at": at,
            "version": self._version("risk_clue", clue_id),
            "payload": payload,
        }
        return self._emit(event, actor=actor or source_type, decision="register_clue")

    def open_handoff(self, event_id: str, handoff_id: str, from_agency: str, to_agency: str,
                     due_at: str, refs: tuple[str, ...], at: str) -> dict[str, Any]:
        event = {
            "event_id": event_id,
            "event_type": "HANDOFF_OPENED",
            "aggregate_type": "agency_handoff",
            "aggregate_id": handoff_id,
            "occurred_at": at,
            "version": self._version("agency_handoff", handoff_id),
            "payload": {
                "handoff_id": handoff_id, "from_agency": from_agency, "to_agency": to_agency,
                "due_at": due_at, "refs": list(refs),
            },
        }
        return self._emit(event, actor=from_agency, decision="open_handoff", target=handoff_id)

    def decide_handoff(self, event_id: str, handoff_id: str, agency_id: str, accept: bool,
                       at: str, reason: str | None = None, due_at: str | None = None) -> dict[str, Any]:
        handoff = self.handoffs.get(handoff_id)
        if handoff is None:
            raise IllegalState(f"移送不存在: {handoff_id}")
        if handoff.to_agency != agency_id:
            raise JurisdictionError("只有接收部门可以决定接收或退回")
        if handoff.status != "opened":
            raise HandoffClosed(f"移送已{handoff.status}，不能重复决定")
        if accept:
            event = {
                "event_id": event_id,
                "event_type": "HANDOFF_ACCEPTED",
                "aggregate_type": "agency_handoff",
                "aggregate_id": handoff_id,
                "occurred_at": at,
                "version": self._version("agency_handoff", handoff_id),
                "payload": {"agency_id": agency_id,
                            "due_at": due_at or handoff.due_at},
            }
            return self._emit(event, actor=agency_id, decision="accept_handoff",
                              target=handoff_id)
        if not reason:
            raise ContractViolation(["payload.reason: 退回移送必须说明理由"])
        event = {
            "event_id": event_id,
            "event_type": "HANDOFF_REJECTED",
            "aggregate_type": "agency_handoff",
            "aggregate_id": handoff_id,
            "occurred_at": at,
            "version": self._version("agency_handoff", handoff_id),
            "payload": {"agency_id": agency_id, "reason": reason},
        }
        stored = self._emit(event, actor=agency_id, decision="reject_handoff",
                            target=handoff_id)
        return stored

    def confirm_fact(self, event_id: str, clue_id: str, agency_id: str, fact_type: str,
                     refs: tuple[str, ...], at: str, result: str = "confirmed") -> dict[str, Any]:
        self._require_mandate(agency_id, fact_type)
        event = {
            "event_id": event_id,
            "event_type": "FACT_CONFIRMED",
            "aggregate_type": "risk_clue",
            "aggregate_id": clue_id,
            "occurred_at": at,
            "version": self._version("risk_clue", clue_id),
            "payload": {
                "fact_id": f"fact-{event_id}", "agency_id": agency_id,
                "fact_type": fact_type, "refs": list(refs), "result": result,
            },
        }
        return self._emit(event, actor=agency_id, decision="confirm_fact", target=clue_id,
                          audit_detail={"fact_type": fact_type, "refs": list(refs)})

    def submit_evidence(self, event_id: str, evidence_id: str, submitted_by: str,
                        refs: tuple[str, ...], at: str) -> dict[str, Any]:
        event = {
            "event_id": event_id,
            "event_type": "EVIDENCE_SUBMITTED",
            "aggregate_type": "risk_clue",
            "aggregate_id": evidence_id,
            "occurred_at": at,
            "version": self._version("risk_clue", evidence_id),
            "payload": {"evidence_id": evidence_id, "submitted_by": submitted_by,
                        "refs": list(refs)},
        }
        return self._emit(event, actor=submitted_by, decision="submit_evidence",
                          target=evidence_id)

    def finalize_evidence(self, event_id: str, evidence_id: str, reviewer: str,
                          decision: str, at: str) -> dict[str, Any]:
        evidence = self.evidence.get(evidence_id)
        if evidence is None:
            raise IllegalState("证据尚未提交，不能终审")
        if evidence.reviewer is not None:
            raise IllegalState("证据已经终审")
        if reviewer == evidence.submitted_by:
            raise ReviewerConflict("提交人不得为自己提交的证据作终审")
        if decision not in ("approved", "rejected"):
            raise ContractViolation(["payload.decision: 终审结论必须是 approved 或 rejected"])
        event = {
            "event_id": event_id,
            "event_type": "EVIDENCE_FINALIZED",
            "aggregate_type": "risk_clue",
            "aggregate_id": evidence_id,
            "occurred_at": at,
            "version": self._version("risk_clue", evidence_id),
            "payload": {"evidence_id": evidence_id, "reviewer": reviewer, "decision": decision},
        }
        return self._emit(event, actor=reviewer, decision="finalize_evidence",
                          target=evidence_id, audit_detail={"decision": decision})

    def record_callback(self, event_id: str, platform: str, callback_id: str, subject_no: str,
                        fingerprint: str, observed_at: str, at: str, url: str = "",
                        listing_id: str | None = None) -> dict[str, Any]:
        subject_no = self._resolve_subject(subject_no)
        listing_id = listing_id or f"listing-{platform}-{subject_no}"
        event = {
            "event_id": event_id,
            "event_type": "CALLBACK_RECORDED",
            "aggregate_type": "platform_listing",
            "aggregate_id": listing_id,
            "occurred_at": at,
            "version": self._version("platform_listing", listing_id),
            "payload": {
                "platform": platform, "callback_id": callback_id, "subject_no": subject_no,
                "content_fingerprint": fingerprint, "observed_at": observed_at, "url": url,
            },
        }
        return self._emit(event, actor=f"platform:{platform}", decision="record_callback",
                          target=listing_id)

    def restrict_scope(self, event_id: str, action_id: str, agency_id: str, scope_type: str,
                       scope_ref: str, at: str, emergency: bool = True,
                       based_on: tuple[str, ...] = (), legal_basis: str | None = None,
                       refs: tuple[str, ...] = (), action_type: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "scope_type": scope_type, "scope_ref": scope_ref, "agency_id": agency_id,
            "emergency": emergency, "based_on": list(based_on), "refs": list(refs),
        }
        if legal_basis:
            payload["legal_basis"] = legal_basis
        if action_type:
            payload["action_type"] = action_type
        event = {
            "event_id": event_id,
            "event_type": "SCOPE_RESTRICTED",
            "aggregate_type": "enforcement_action",
            "aggregate_id": action_id,
            "occurred_at": at,
            "version": self._version("enforcement_action", action_id),
            "payload": payload,
        }
        return self._emit(event, actor=agency_id,
                          decision="emergency_restrict" if emergency else "restrict_scope",
                          target=action_id)

    def take_statutory_action(self, event_id: str, action_id: str, agency_id: str,
                              action_type: str, legal_basis: str, at: str,
                              refs: tuple[str, ...] = (), based_on: tuple[str, ...] = (),
                              lifts: tuple[str, ...] = (), ratifies: tuple[str, ...] = (),
                              scope_type: str = "chain", scope_ref: str = "",
                              rectification_required: bool = True,
                              restrictive: bool = False) -> dict[str, Any]:
        event = {
            "event_id": event_id,
            "event_type": "STATUTORY_ACTION_TAKEN",
            "aggregate_type": "enforcement_action",
            "aggregate_id": action_id,
            "occurred_at": at,
            "version": self._version("enforcement_action", action_id),
            "payload": {
                "action_type": action_type, "legal_basis": legal_basis, "refs": list(refs),
                "based_on": list(based_on), "lifts": list(lifts),
                "ratifies": list(ratifies), "agency_id": agency_id,
                "scope_type": scope_type, "scope_ref": scope_ref,
                "rectification_required": rectification_required,
                "restrictive": restrictive,
            },
        }
        return self._emit(event, actor=agency_id, decision="statutory_action",
                          target=action_id, audit_detail={"action_type": action_type})

    def conclude_test(self, event_id: str, sample_id: str, agency_id: str, conclusion: str,
                      at: str) -> dict[str, Any]:
        self._require_mandate(agency_id, "test_conclusion")
        event = {
            "event_id": event_id,
            "event_type": "TEST_CONCLUDED",
            "aggregate_type": "test_sample",
            "aggregate_id": sample_id,
            "occurred_at": at,
            "version": self._version("test_sample", sample_id),
            "payload": {"sample_id": sample_id, "conclusion": conclusion},
        }
        return self._emit(event, actor=agency_id, decision="conclude_test", target=sample_id)

    def correct_test(self, event_id: str, sample_id: str, agency_id: str, new_conclusion: str,
                     reason: str, at: str) -> list[dict[str, Any]]:
        """更正检测结论；只重开真正依赖该样本的处置。"""
        self._require_mandate(agency_id, "test_conclusion")
        sample = self.samples.get(sample_id)
        if sample is None or sample.conclusion is None:
            raise IllegalState("样本尚未出具结论，不能更正")
        old_conclusion = sample.conclusion
        emitted: list[dict[str, Any]] = []
        event = {
            "event_id": event_id,
            "event_type": "TEST_CORRECTED",
            "aggregate_type": "test_sample",
            "aggregate_id": sample_id,
            "occurred_at": at,
            "version": self._version("test_sample", sample_id),
            "payload": {
                "sample_id": sample_id, "old_conclusion": old_conclusion,
                "new_conclusion": new_conclusion, "reason": reason,
            },
        }
        emitted.append(self._emit(event, actor=agency_id, decision="correct_test",
                                  target=sample_id, audit_detail={"reason": reason}))

        risk_cleared = _is_positive(old_conclusion) and _is_qualified(new_conclusion)
        if risk_cleared:
            for action_id in sorted(self._sample_dependents.get(sample_id, set())):
                action = self.actions[action_id]
                if action.status == "lifted":
                    continue
                # 紧急措施已被法定措施替代（解除）时不重开；只重开仍生效、
                # 且确实以该样本为依据的处置。
                if action.kind == "emergency" and action.statutory_successor is not None:
                    continue
                reopen_event = {
                    "event_id": f"{event_id}-reopen-{action_id}",
                    "event_type": "ACTION_REOPENED",
                    "aggregate_type": "enforcement_action",
                    "aggregate_id": action_id,
                    "occurred_at": at,
                    "version": self._version("enforcement_action", action_id),
                    "payload": {
                        "reason": f"依赖样本 {sample_id} 的结论由 {old_conclusion} "
                                  f"更正为 {new_conclusion}：{reason}"
                    },
                }
                emitted.append(self._emit(reopen_event, actor=agency_id,
                                          decision="reopen_action", target=action_id))
        return emitted

    def submit_rectification(self, event_id: str, rectification_id: str, submitted_by: str,
                             refs: tuple[str, ...], at: str,
                             review_due_at: str | None = None) -> dict[str, Any]:
        event = {
            "event_id": event_id,
            "event_type": "RECTIFICATION_SUBMITTED",
            "aggregate_type": "rectification",
            "aggregate_id": rectification_id,
            "occurred_at": at,
            "version": self._version("rectification", rectification_id),
            "payload": {
                "rectification_id": rectification_id, "submitted_by": submitted_by,
                "refs": list(refs), "review_due_at": review_due_at,
            },
        }
        return self._emit(event, actor=submitted_by, decision="submit_rectification",
                          target=rectification_id)

    def review_rectification(self, event_id: str, rectification_id: str, reviewer: str,
                             passed: bool, at: str) -> dict[str, Any]:
        rect = self.rectifications.get(rectification_id)
        if rect is None:
            raise IllegalState("整改尚未提交，不能复查")
        if rect.reviewed_at is not None:
            raise IllegalState("整改已经复查")
        if reviewer == rect.submitted_by:
            raise ReviewerConflict("复查人不得是整改提交人")
        self._require_mandate(reviewer, "rectification_review")
        event = {
            "event_id": event_id,
            "event_type": "RECTIFICATION_REVIEWED",
            "aggregate_type": "rectification",
            "aggregate_id": rectification_id,
            "occurred_at": at,
            "version": self._version("rectification", rectification_id),
            "payload": {
                "rectification_id": rectification_id, "reviewer": reviewer, "passed": passed,
            },
        }
        return self._emit(event, actor=reviewer, decision="review_rectification",
                          target=rectification_id, audit_detail={"passed": passed})

    def scan_overdue(self, now: str) -> list[dict[str, Any]]:
        """按原始期限推进待接收、待复查与问责；故障恢复后调用结果一致。"""
        flagged: list[dict[str, Any]] = []
        now_dt = parse_ts(now)

        def flag(event_id: str, ref_type: str, ref_id: str, actor: str, reason: str) -> None:
            key = (ref_type, ref_id)
            if key in self.overdue and self.overdue[key]["resolved_at"] is None:
                return
            if key in self.overdue:  # 已解决的不重复问责
                return
            event = {
                "event_id": event_id,
                "event_type": "OVERDUE_FLAGGED",
                "aggregate_type": ref_type,
                "aggregate_id": ref_id,
                "occurred_at": now,
                "version": self._version(ref_type, ref_id),
                "payload": {"ref_type": ref_type, "ref_id": ref_id, "reason": reason},
            }
            stored = self._emit(event, actor="FOOD_SAFETY_OFFICE",
                                decision="flag_overdue", target=ref_id,
                                audit_detail={"reason": reason})
            flagged.append(stored)

        for handoff_id, handoff in self.handoffs.items():
            if handoff.status == "opened" and parse_ts(handoff.due_at) < now_dt:
                flag(f"overdue-handoff-{handoff_id}", "agency_handoff", handoff_id,
                     handoff.to_agency, "跨部门移送逾期未接收")
        for rect_id, rect in self.rectifications.items():
            if rect.reviewed_at is None and rect.review_due_at and parse_ts(rect.review_due_at) < now_dt:
                flag(f"overdue-rect-{rect_id}", "rectification", rect_id,
                     rect.submitted_by, "整改复查逾期")
        return flagged

    def close_case(self, event_id: str, clue_id: str, actor: str, at: str) -> dict[str, Any]:
        status = self.chain_status(clue_id)
        if status["blockers"]:
            raise IllegalState("风险链未闭合，不能结案：" + "；".join(
                b["reason"] for b in status["blockers"]
            ))
        event = {
            "event_id": event_id,
            "event_type": "CASE_CLOSED",
            "aggregate_type": "risk_clue",
            "aggregate_id": clue_id,
            "occurred_at": at,
            "version": self._version("risk_clue", clue_id),
            "payload": {},
        }
        return self._emit(event, actor=actor, decision="close_case", target=clue_id)

    # ------------------------------------------------------------------ 查询

    def _chain_entities(self, clue: Clue) -> dict[str, set[str]]:
        """从线索出发遍历生效中的关系图，按节点类型收集中文编号集合。"""
        groups: dict[str, set[str]] = {
            "licensed_premise": set(), "platform_listing": set(),
            "material_batch": set(), "test_sample": set(),
        }
        subject_no = clue.subject_ref

        def add_ref(ref: str) -> None:
            if ref in self.premises:
                groups["licensed_premise"].add(ref)
            elif ref in self.listing_by_id:
                groups["platform_listing"].add(ref)
            elif ref in self.batches:
                groups["material_batch"].add(ref)
            elif ref in self.samples:
                groups["test_sample"].add(ref)

        for ref in clue.refs:
            add_ref(ref)
        groups["licensed_premise"].update(
            p.premise_id for p in self.premises.values() if p.subject_no == subject_no
        )
        groups["platform_listing"].update(
            l.listing_id for l in self.listing_by_id.values() if l.subject_no == subject_no
        )
        # 批次与场所/页面互相挂接，样本挂在批次下。
        changed = True
        while changed:
            changed = False
            for batch_id, batch in self.batches.items():
                linked = any(
                    ref in groups["licensed_premise"] or ref in groups["platform_listing"]
                    or ref == subject_no
                    for ref in batch.links
                ) or batch_id in groups["material_batch"]
                if linked and batch_id not in groups["material_batch"]:
                    groups["material_batch"].add(batch_id)
                    changed = True
            for sample in self.samples.values():
                if sample.batch_id in groups["material_batch"] and sample.sample_id not in groups["test_sample"]:
                    groups["test_sample"].add(sample.sample_id)
                    changed = True
            for fact in self.facts.values():
                if fact.clue_id != clue.clue_id:
                    continue
                for ref in fact.refs:
                    before = {k: len(v) for k, v in groups.items()}
                    add_ref(ref)
                    after = {k: len(v) for k, v in groups.items()}
                    if before != after:
                        changed = True
        return groups

    def _actions_covering(self, ref: str) -> list[Action]:
        return [
            a for a in self.actions.values()
            if a.scope_ref == ref or ref in a.refs
        ]

    def chain_status(self, clue_id: str) -> dict[str, Any]:
        """回答：卡在哪个部门、哪些商品/店铺受限、全链闭合还缺什么证据。"""
        clue = self.clues[clue_id]
        groups = self._chain_entities(clue)
        blockers: list[dict[str, str]] = []
        chain_facts = [f for f in self.facts.values() if f.clue_id == clue_id]

        # 1. 移送必须有明确接收（退回则退回部门要重新改派）。
        for handoff_id, handoff in self.handoffs.items():
            if not any(any(r in g for r in handoff.refs) for g in groups.values()) and not (
                clue.subject_ref in handoff.refs
            ):
                continue
            if handoff.status == "opened":
                blockers.append({
                    "agency": handoff.to_agency, "stage": "待接收",
                    "reason": f"移送 {handoff_id} 等待 {handoff.to_agency} 接收或退回",
                })
            elif handoff.status == "rejected":
                covered = any(
                    other.status == "accepted"
                    and other.handoff_id != handoff_id
                    and set(other.refs) & set(handoff.refs)
                    for other in self.handoffs.values()
                )
                if not covered:
                    blockers.append({
                        "agency": handoff.from_agency, "stage": "待改派",
                        "reason": f"移送 {handoff_id} 被退回（{handoff.reason}），需重新移送",
                    })

        # 2. 指纹/地址/许可证变化的节点必须隔离核验后才能继续；
        #    已经职责部门现场认定为违法的，转入法定处置轨道，不再挂“待核验”。
        violation_refs = {
            ref
            for fact in chain_facts if fact.result == "violation"
            for ref in fact.refs
        }
        for ref in sorted(groups["platform_listing"]):
            listing = self.listing_by_id[ref]
            if listing.quarantined and ref not in violation_refs:
                reason = (
                    f"平台页面 {ref} 内容指纹变化，已隔离待核验"
                    if listing.fingerprint_changed
                    else f"平台页面 {ref} 信息来自平台回调，尚未核验"
                )
                blockers.append({
                    "agency": "MARKET_REGULATION", "stage": "待核验", "reason": reason,
                })
        for ref in sorted(groups["licensed_premise"]):
            premise = self.premises[ref]
            if premise.quarantined and ref not in violation_refs:
                blockers.append({
                    "agency": "MARKET_REGULATION", "stage": "待核验",
                    "reason": f"场所 {ref}（{premise.address}）地址或许可证变化，已隔离待核验",
                })

        # 3. 每类节点要有职责部门确认的事实。
        confirmed_refs: dict[str, list[Fact]] = {}
        for fact in chain_facts:
            for ref in fact.refs:
                confirmed_refs.setdefault(ref, []).append(fact)

        quarantined_refs = {
            ref for ref in groups["platform_listing"]
            if self.listing_by_id[ref].quarantined and ref not in violation_refs
        } | {
            ref for ref in groups["licensed_premise"]
            if self.premises[ref].quarantined and ref not in violation_refs
        }
        for kind in ("licensed_premise", "platform_listing", "material_batch"):
            for ref in sorted(groups[kind]):
                # 已在隔离核验轨道上的节点不再重复挂“待现场检查”。
                if ref in quarantined_refs:
                    continue
                if not confirmed_refs.get(ref):
                    blockers.append({
                        "agency": ENTITY_OWNER[kind], "stage": "待现场检查",
                        "reason": f"{kind} {ref} 尚无职责部门确认的事实",
                    })

        # 4. 样本要有检测结论；阳性结果要有处置覆盖批次。
        for sample_id in sorted(groups["test_sample"]):
            sample = self.samples[sample_id]
            if sample.conclusion is None:
                blockers.append({
                    "agency": "INSPECTION", "stage": "待检测",
                    "reason": f"样本 {sample_id} 尚未出具检测结论",
                })
                continue
            if _is_positive(sample.conclusion):
                covered = any(
                    a.kind == "statutory" and a.status != "reopened"
                    and (sample.batch_id in a.refs or sample_id in a.based_on
                         or sample.batch_id in a.based_on)
                    for a in self.actions.values()
                )
                if not covered:
                    blockers.append({
                        "agency": "MARKET_REGULATION", "stage": "待法定处置",
                        "reason": f"样本 {sample_id} 结论 {sample.conclusion}，批次 "
                                  f"{sample.batch_id} 尚无生效法定处置",
                    })

        # 5. 现场认定违法（虚假地址、无证、页面冒名等）必须有生效法定处置覆盖该节点。
        statutory_refs = {
            ref
            for action in self.actions.values()
            if action.kind == "statutory" and action.status != "reopened"
            for ref in [action.scope_ref, *action.refs]
        }
        for fact in chain_facts:
            if fact.result != "violation":
                continue
            for ref in fact.refs:
                if ref not in statutory_refs:
                    blockers.append({
                        "agency": fact.agency_id, "stage": "待法定处置",
                        "reason": f"{fact.fact_type} 已认定 {ref} 违法，尚无法定处置覆盖",
                    })

        # 6. 紧急处置不能替代法定程序；被更正重开的处置要重新闭合。
        chain_entities = (
            groups["licensed_premise"] | groups["platform_listing"]
            | groups["material_batch"] | groups["test_sample"]
        )
        chain_units = chain_entities | {clue.subject_ref, clue_id}
        chain_action_ids = {
            a.action_id for a in self.actions.values()
            if any(ref in chain_units for ref in [a.scope_ref, *a.refs, *a.based_on])
        }
        chain_units |= chain_action_ids
        for action_id, action in self.actions.items():
            touches_chain = any(
                ref in chain_units
                for ref in [action.scope_ref, *action.refs, *action.based_on]
            )
            if not touches_chain:
                continue
            if action.status == "reopened":
                blockers.append({
                    "agency": action.agency_id, "stage": "待重新处置",
                    "reason": f"处置 {action_id} 已随证据更正重开：{action.reopen_reasons[-1]}",
                })
            if action.kind == "emergency" and action.statutory_successor is None and action.status != "lifted":
                blockers.append({
                    "agency": action.agency_id, "stage": "待法定追认",
                    "reason": f"紧急处置 {action_id}（{action.action_type}）须补充法定程序",
                })

        # 7. 处置引用的证据必须终审通过，且提交人未自审。
        for evidence in self.evidence.values():
            in_chain = any(
                r in groups["licensed_premise"] | groups["platform_listing"]
                | groups["material_batch"] | groups["test_sample"]
                for r in evidence.refs
            )
            if not in_chain:
                continue
            if evidence.decision is None:
                blockers.append({
                    "agency": "FOOD_SAFETY_OFFICE", "stage": "待证据终审",
                    "reason": f"证据 {evidence.evidence_id} 尚未终审",
                })
            elif evidence.decision == "rejected":
                blockers.append({
                    "agency": "FOOD_SAFETY_OFFICE", "stage": "证据不成立",
                    "reason": f"证据 {evidence.evidence_id} 终审未通过，需补充证据",
                })

        # 8. 整改必须经复查；无法定处置对应的整改记录则链条还缺整改环节；
        #    未通过则重新整改。
        rects_by_action: dict[str, list[Rectification]] = {}
        for rect in self.rectifications.values():
            for ref in rect.refs:
                rects_by_action.setdefault(ref, []).append(rect)
        for action_id, action in self.actions.items():
            if (action.kind != "statutory" or action.status == "lifted"
                    or not action.rectification_required):
                continue
            touches_chain = any(
                ref in chain_entities or ref == clue.subject_ref
                for ref in [action.scope_ref, *action.refs, *action.based_on]
            )
            if not touches_chain:
                continue
            rects = rects_by_action.get(action_id, [])
            if not rects:
                blockers.append({
                    "agency": action.agency_id, "stage": "待整改",
                    "reason": f"法定处置 {action_id}（{action.action_type}）尚无整改提交",
                })
        for rect_id, rect in self.rectifications.items():
            if not any(r in self.actions for r in rect.refs):
                continue
            if rect.reviewed_at is None:
                blockers.append({
                    "agency": "MARKET_REGULATION", "stage": "待复查",
                    "reason": f"整改 {rect_id} 已提交，等待复查",
                })
            elif rect.passed is False:
                blockers.append({
                    "agency": rect.submitted_by, "stage": "整改不合格",
                    "reason": f"整改 {rect_id} 复查未通过，需重新整改",
                })

        # 9. 逾期未消解要问责并阻断结案（仅本链涉及的移送与整改）。
        chain_handoff_ids = {h.handoff_id for h in self.handoffs.values()
                             if any(any(r in g for r in h.refs) for g in groups.values())
                             or clue.subject_ref in h.refs}
        chain_rect_ids = {
            rect.rectification_id for rect in self.rectifications.values()
            if any(ref in chain_units for ref in rect.refs)
        }
        for (ref_type, ref_id), overdue in self.overdue.items():
            if ref_type == "agency_handoff" and ref_id not in chain_handoff_ids:
                continue
            if ref_type == "rectification" and ref_id not in chain_rect_ids:
                continue
            if overdue["resolved_at"] is None:
                blockers.append({
                    "agency": "FOOD_SAFETY_OFFICE", "stage": "逾期问责",
                    "reason": f"{ref_type} {ref_id} 已逾期待办，须问责并办结",
                })

        restrictions = []
        for action in sorted(self.actions.values(), key=lambda x: x.at):
            if not action.restrictive or action.status == "lifted":
                continue
            covers = {r for r in [action.scope_ref, *action.refs]
                      if r in self.premises or r in self.listing_by_id
                      or r in self.batches or r in self.subjects}
            restrictions.append({
                "action_id": action.action_id, "kind": action.kind,
                "scope_type": action.scope_type, "scope_ref": action.scope_ref,
                "covers": sorted(covers),
                "action_type": action.action_type, "status": action.status, "at": action.at,
            })

        blocker_order = {"待接收": 0, "待改派": 1, "待核验": 2, "待现场检查": 3,
                         "待检测": 4, "待法定处置": 5, "待法定追认": 6, "待重新处置": 7,
                         "待证据终审": 8, "证据不成立": 9, "待整改": 10, "待复查": 11,
                         "整改不合格": 12, "逾期问责": 13}
        blockers.sort(key=lambda b: (blocker_order.get(b["stage"], 99), b["reason"]))

        return {
            "clue_id": clue_id,
            "closed": clue.closed_at is not None and not blockers,
            "closed_at": clue.closed_at if clue.closed_at and not blockers else None,
            "was_closed_at": clue.closed_at,
            "current_department": blockers[0]["agency"] if blockers else None,
            "current_stage": blockers[0]["stage"] if blockers else "已闭合",
            "blockers": blockers,
            "restricted": restrictions,
            "entities": {k: sorted(v) for k, v in groups.items()},
        }
