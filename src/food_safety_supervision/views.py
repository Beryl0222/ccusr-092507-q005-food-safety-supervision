"""公开视图与监管视图。

公开视图隐藏举报人身份与办案细节，只呈现处置进展与受限范围；
监管视图保留完整事实、证据与审计信息，并且每次访问都写入审计日志。
"""

from __future__ import annotations

from typing import Any

from .service import SupervisionService

# 面向公众的阶段措辞：保留卡点，不暴露内部分工与侦查细节。
PUBLIC_STAGE_TEXT = {
    "待接收": "跨部门协同处理中",
    "待改派": "跨部门协同处理中",
    "待核验": "信息核验中",
    "待现场检查": "现场核查中",
    "待检测": "检验检测中",
    "待法定处置": "依法处置中",
    "待法定追认": "依法处置中",
    "待重新处置": "依据新证据重新处置中",
    "待证据终审": "材料审核中",
    "证据不成立": "补充核查中",
    "待复查": "整改复查中",
    "整改不合格": "继续整改中",
    "逾期问责": "督办处理中",
    "已闭合": "该风险已全链闭合",
}

# 公安侦查类事实不对公开视图暴露。
CONFIDENTIAL_FACT_TYPES = {"investigation_clue"}


def public_view(service: SupervisionService, clue_id: str) -> dict[str, Any]:
    """无需身份的公开视图：不记访问审计，也不暴露办案信息。"""
    status = service.chain_status(clue_id)
    clue = service.clues[clue_id]
    subject = service.subjects.get(clue.subject_ref)

    restrictions = [
        {
            "scope_type": item["scope_type"],
            "scope_ref": item["scope_ref"],
            "action_type": item["action_type"],
            "status": "生效中" if item["status"] != "lifted" else "已解除",
        }
        for item in status["restricted"]
    ]

    blockers = [
        {
            "stage": blocker["stage"],
            "public_stage": PUBLIC_STAGE_TEXT.get(blocker["stage"], "处理中"),
            "subject": _public_subject(blocker["reason"]),
        }
        for blocker in status["blockers"]
    ]

    return {
        "clue_id": clue_id,
        "subject_name": subject.name if subject else None,
        "progress": status["current_stage"] if status["current_stage"] else "已闭合",
        "public_progress": PUBLIC_STAGE_TEXT.get(status["current_stage"], "处理中"),
        "closed": status["closed"],
        "restricted_goods_and_shops": restrictions,
        "pending_steps": blockers,
    }


def _public_subject(reason: str) -> str:
    """内部原因中可能出现编号；保留对象编号但去掉部门与问责措辞。"""
    for needle in ("MARKET_REGULATION", "AGRICULTURE", "INSPECTION",
                   "PUBLIC_SECURITY", "FOOD_SAFETY_OFFICE"):
        reason = reason.replace(needle, "主管部门")
    return reason


def regulator_view(service: SupervisionService, clue_id: str, viewer: str) -> dict[str, Any]:
    """监管视图：返回全链细节，并把本次访问写入审计。"""
    status = service.chain_status(clue_id)
    clue = service.clues[clue_id]
    groups = status["entities"]

    facts = [
        {
            "fact_id": fact.fact_id,
            "agency_id": fact.agency_id,
            "fact_type": fact.fact_type,
            "refs": fact.refs,
            "result": fact.result,
        }
        for fact in service.facts.values()
        if fact.clue_id == clue_id
    ]

    evidence = []
    chain_refs = set().union(*groups.values()) if any(groups.values()) else set()
    chain_refs.add(clue.subject_ref)
    for item in service.evidence.values():
        if not any(ref in chain_refs for ref in item.refs):
            continue
        evidence.append({
            "evidence_id": item.evidence_id,
            "submitted_by": item.submitted_by,
            "reviewer": item.reviewer,
            "decision": item.decision,
            "reviewed_at": item.reviewed_at,
            "refs": item.refs,
        })

    handoffs = [
        {
            "handoff_id": h.handoff_id,
            "from_agency": h.from_agency,
            "to_agency": h.to_agency,
            "status": h.status,
            "due_at": h.due_at,
            "decided_at": h.decided_at,
            "reason": h.reason,
            "refs": h.refs,
        }
        for h in service.handoffs.values()
        if any(any(r in g for r in h.refs) for g in groups.values())
        or clue.subject_ref in h.refs
    ]

    samples = [
        {
            "sample_id": sample_id,
            "batch_id": service.samples[sample_id].batch_id,
            "conclusion": service.samples[sample_id].conclusion,
            "history": service.samples[sample_id].history,
        }
        for sample_id in groups["test_sample"]
    ]

    overdue = [
        {"ref_type": ref_type, "ref_id": ref_id, **detail}
        for (ref_type, ref_id), detail in service.overdue.items()
    ]

    service.audit.access(
        viewer, "view_regulator_chain", clue_id,
        closed=status["closed"], blockers=len(status["blockers"]),
    )

    return {
        "clue_id": clue_id,
        "reporter": clue.reporter,
        "subject_ref": clue.subject_ref,
        "subject_name": service.subjects.get(clue.subject_ref, None)
        and service.subjects[clue.subject_ref].name,
        "closed": status["closed"],
        "closed_at": status["closed_at"],
        "current_department": status["current_department"],
        "current_stage": status["current_stage"],
        "blockers": status["blockers"],
        "entities": groups,
        "restricted": status["restricted"],
        "facts": facts,
        "evidence": evidence,
        "handoffs": handoffs,
        "samples": samples,
        "overdue": overdue,
    }
