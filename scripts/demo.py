#!/usr/bin/env python3
"""端到端演示：幽灵外卖店铺 + 运输环节异常蔬菜的协同督办全链。

运行：
    PYTHONPATH=src python3 scripts/demo.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from food_safety_supervision import (
    ClosureBlocked,
    DuplicateConflict,
    EventStore,
    SupervisionService,
)
from food_safety_supervision import queries, scheduler


def show(title: str, data: object) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    svc = SupervisionService(EventStore())
    T0 = "2026-10-01T09:00:00+08:00"

    # 各部门只能确认职责范围内的事实
    svc.declare_duty("market_regulation", "PLATFORM_SUPERVISION", at=T0)
    svc.declare_duty("agriculture", "CIRCULATION_INSPECTION", at=T0)
    svc.declare_duty("public_security", "CRIMINAL_CLUE", at=T0)

    # 统一主体号把平台、现场、抽检、公安各自的对象编号串成一张图
    subject = svc.register_subject(
        "S001", subject_no="91310000MA1GHOST", name="幽灵餐饮管理有限公司",
        profile_fingerprint="addr-查无此址|lic-待核|page-v1", at=T0)
    premise = svc.register_node(
        "licensed_premise", "P001", address="某路1号（实地查无此址）",
        license_no="JY-0001", at=T0)
    listing = svc.register_node(
        "platform_listing", "L001", platform="某团",
        listing_name="幽灵外卖·黄焖鸡旗舰店", content_fingerprint="page-v1", at=T0)
    batch = svc.register_node(
        "material_batch", "B001", batch_no="VEG-20261001", product="菠菜",
        carrier="沪A·12345", at=T0)
    sample = svc.register_node(
        "test_sample", "T001", sample_no="JC-0001", batch_ref="material_batch:B001", at=T0)

    svc.link("OPERATES_AT", subject, premise, at=T0)
    svc.link("PUBLISHED_AS", subject, listing, at=T0)
    svc.link("USES_BATCH", subject, batch, at=T0)
    svc.link("SAMPLED_FROM", sample, batch, at=T0)

    clue = svc.register_clue(
        "C001", source_type="PLATFORM_REPORT", subject_ref=subject,
        reporter="王某（信息仅监管视图可见）", reporter_contact="138****0000",
        case_detail="无真实经营场所；运输蔬菜农残异常，疑似供餐同一主体", at=T0)

    # 跨部门移送：市场监管 -> 公安，必须明确接收或退回
    svc.create_handoff(
        "H001", from_agency="market_regulation", to_agency="public_security",
        due_at="2026-10-03T09:00:00+08:00", clue_ref=clue, at=T0)
    svc.accept_handoff(
        "agency_handoff:H001", agency_id="public_security",
        handled_due_at="2026-10-06T09:00:00+08:00", at="2026-10-01T11:00:00+08:00")

    # 紧急下架先阻止流通
    svc.restrict_scope(
        "LISTING", listing, agency_id="market_regulation",
        reason="无真实经营场所，页面信息疑似虚假", emergency=True,
        clue_ref=clue, at="2026-10-01T10:00:00+08:00")
    svc.restrict_scope(
        "BATCH", batch, agency_id="agriculture",
        reason="运输环节农残快速检测异常，整批封存", emergency=True,
        clue_ref=clue, at="2026-10-01T10:30:00+08:00")

    # 证据提交与终审（提交人不得自审）
    ev_site = svc.submit_evidence(
        "E001", evidence_kind="SITE_INSPECTION", submitted_by="inspector_zhao",
        clue_ref=clue, at="2026-10-01T14:00:00+08:00")
    svc.review_evidence(ev_site, reviewer="market_regulation",
                        verdict="APPROVED", at="2026-10-01T16:00:00+08:00")
    ev_lab = svc.submit_evidence(
        "E002", evidence_kind="LAB_REPORT", submitted_by="lab_chen",
        clue_ref=clue, at="2026-10-01T15:00:00+08:00")
    svc.review_evidence(ev_lab, reviewer="agriculture",
                        verdict="APPROVED", at="2026-10-02T09:00:00+08:00")

    # 法定处置覆盖被紧急限制的两个范围
    a_site = svc.open_enforcement(
        "A001", agency_id="public_security", basis_refs=[ev_site, ev_lab],
        clue_ref=clue, scope=("LISTING", listing), at="2026-10-02T10:00:00+08:00")
    a_batch = svc.open_enforcement(
        "A002", agency_id="agriculture", basis_refs=[ev_lab],
        clue_ref=clue, scope=("BATCH", batch), at="2026-10-02T10:30:00+08:00")

    # 此时尝试结案：整改复查缺失、紧急措施无法定结论、限制未解除
    try:
        svc.close_case(clue, closed_by="market_regulation",
                       at="2026-10-02T11:00:00+08:00")
    except ClosureBlocked as exc:
        show("① 结案被闸口拦截——全链闭合还缺少这些证据/动作", exc.blockers)

    # 办结法定处置（不替代后续整改复查）
    svc.resolve_enforcement(
        a_site, agency_id="public_security",
        resolution="查实无证经营，行政处罚并移送起诉意见",
        at="2026-10-04T09:00:00+08:00")
    svc.resolve_enforcement(
        a_batch, agency_id="agriculture",
        resolution="不合格批次监督销毁，上游进货查验义务另案查处",
        at="2026-10-04T09:30:00+08:00")

    # 整改与复查
    rect = svc.request_rectification(
        "R001", agency_id="market_regulation", clue_ref=clue,
        due_at="2026-10-08T09:00:00+08:00", at="2026-10-04T10:00:00+08:00")
    ev_rect = svc.submit_evidence(
        "E003", evidence_kind="RECTIFICATION_PHOTO", submitted_by="subject_owner",
        clue_ref=clue, at="2026-10-05T09:00:00+08:00")
    svc.review_evidence(ev_rect, reviewer="market_regulation",
                        verdict="APPROVED", at="2026-10-05T15:00:00+08:00")
    svc.submit_rectification(rect, evidence_ref=ev_rect,
                             submitter="subject_owner",
                             at="2026-10-05T09:00:00+08:00")
    svc.pass_rectification(rect, reviewer="market_regulation",
                           at="2026-10-05T16:00:00+08:00")
    svc.release_scope("LISTING", listing, reason="处罚执行完毕，复查通过",
                      at="2026-10-05T16:30:00+08:00")
    svc.release_scope("BATCH", batch, reason="批次已监督销毁",
                      at="2026-10-04T10:00:00+08:00")

    svc.close_case(clue, closed_by="market_regulation",
                   at="2026-10-06T09:00:00+08:00")
    show("② 全链闭合后的风险状态（卡在哪个部门/受限范围/缺证）",
         queries.risk_status(svc, clue, "2026-10-06T09:00:00+08:00"))
    show("③ 公开视图（举报人、办案细节已隐藏）",
         queries.public_view(svc, clue, "2026-10-06T09:00:00+08:00"))

    # 检测结论更正：只重开真正依赖它的处置（批次处置与页面处置都引用 E002）
    new_ev = svc.correct_evidence(
        ev_lab, new_evidence_id="E002-V2", submitted_by="lab_chen",
        correction_reason="复检发现样本编号错配，农残实际超标且含禁用农药",
        at="2026-10-09T09:00:00+08:00")
    show("④ 检测结论更正后的卡点与缺证（案件被自动重开）",
         queries.risk_status(svc, clue, "2026-10-09T09:30:00+08:00"))
    svc.review_evidence(new_ev, reviewer="agriculture", verdict="APPROVED",
                        at="2026-10-09T11:00:00+08:00")

    # 平台回调重放：同号幂等，同号不同内容冲突
    callback = {
        "event_id": "callback-demo-1",
        "event_type": "SUBJECT_REGISTERED",
        "aggregate_type": "regulated_subject",
        "aggregate_id": "CB1",
        "occurred_at": "2026-10-09T12:00:00+08:00",
        "version": 1,
        "payload": {"subject_no": "CB-1", "name": "回调店铺",
                    "profile_fingerprint": "fp1"},
    }
    first = svc.ingest_callback(dict(callback))
    replay = svc.ingest_callback(dict(callback))
    try:
        svc.ingest_callback(dict(callback, payload=dict(callback["payload"], name="篡改名")))
    except DuplicateConflict as exc:
        conflict = str(exc)
    else:
        conflict = "未检测到冲突（异常）"
    show("⑤ 平台回调：首次写入 / 重放结果 / 同号篡改",
         {"首次写入": first, "重放返回False即幂等": replay, "同号不同内容": conflict})

    # 逾期问责：按原期限推进，重复调度不重复问责
    overdue_clue = svc.register_clue(
        "C900", source_type="INSPECTION", subject_ref=subject,
        case_detail="另一条待接收移送", at="2026-10-09T12:30:00+08:00")
    svc.create_handoff(
        "H900", from_agency="market_regulation", to_agency="agriculture",
        due_at="2026-10-10T09:00:00+08:00", clue_ref=overdue_clue,
        at="2026-10-09T12:30:00+08:00")
    first_run = scheduler.escalate_overdue(svc, "2026-10-12T09:00:00+08:00")
    second_run = scheduler.escalate_overdue(svc, "2026-10-13T09:00:00+08:00")
    show("⑥ 逾期问责：首次问责目标 / 故障恢复后重跑",
         {"首次问责": first_run, "重跑（应为空）": second_run})

    # 监管视图：每次访问与决定都留痕
    queries.regulator_view(svc, clue, viewer="auditor_zhou",
                           purpose="季度督查", at="2026-10-12T10:00:00+08:00")
    logs = [
        {"time": row["accessed_at"], "viewer": row["viewer"],
         "target": row["target"], "decision": row["decision"]}
        for row in svc.store.access_log()
    ]
    show("⑦ 监管视图访问/决定留痕（最近 6 条）", logs[-6:])


if __name__ == "__main__":
    main()
