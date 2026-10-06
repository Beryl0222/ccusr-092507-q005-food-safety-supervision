from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from food_safety_supervision import (
    ClosureBlocked,
    DuplicateConflict,
    EventStore,
    HandoffStateError,
    OutOfJurisdiction,
    ReviewerConflict,
    SubjectQuarantined,
    SupervisionService,
    VersionConflict,
)
from food_safety_supervision import graph, queries, scheduler
from food_safety_supervision.services import closure_blockers
from food_safety_supervision.clock import parse


def make_service(path: str = ":memory:") -> SupervisionService:
    return SupervisionService(EventStore(path))


def build_full_chain(svc: SupervisionService, *, now: str = "2026-10-01T09:00:00+08:00") -> dict:
    """幽灵外卖店铺 + 运输异常蔬菜的完整协同链，闭合到可结案。"""
    t = parse(now)

    def d(hours: int) -> str:
        from datetime import timedelta

        return (t + timedelta(hours=hours)).isoformat()

    # 部门法定职责
    svc.declare_duty("market_regulation", "PLATFORM_SUPERVISION", at=d(0))
    svc.declare_duty("market_regulation", "SUBJECT_SUPERVISION", at=d(0))
    svc.declare_duty("agriculture", "CIRCULATION_INSPECTION", at=d(0))
    svc.declare_duty("public_security", "CRIMINAL_CLUE", at=d(0))

    # 经营主体与许可场所、平台页面
    subject = svc.register_subject(
        "S001", subject_no="91310000MA1GHOST", name="幽灵餐饮管理有限公司",
        profile_fingerprint="fp-addr-a|lic-X|page-v1", at=d(0))
    premise = svc.register_node(
        "licensed_premise", "P001", at=d(0), address="某路1号（实地查无此址）",
        license_no="JY-0001")
    listing = svc.register_node(
        "platform_listing", "L001", at=d(0), platform="某团",
        listing_name="幽灵外卖·黄焖鸡旗舰店", content_fingerprint="page-v1")
    svc.link("OPERATES_AT", subject, premise, at=d(0))
    svc.link("PUBLISHED_AS", subject, listing, at=d(0))

    # 原料流通批次与检测样本（农业部门另一条线发现的异常蔬菜）
    batch = svc.register_node(
        "material_batch", "B001", at=d(0), batch_no="VEG-20261001",
        product="菠菜", carrier="沪A·12345")
    sample = svc.register_node(
        "test_sample", "T001", at=d(0), sample_no="JC-0001", batch_ref=batch)
    svc.link("USES_BATCH", subject, batch, at=d(0))
    svc.link("SAMPLED_FROM", sample, batch, at=d(0))

    # 风险线索（举报人信息只进监管视图）
    clue = svc.register_clue(
        "C001", source_type="PLATFORM_REPORT", subject_ref=subject,
        reporter="王某", reporter_contact="138****0000",
        case_detail="平台发现该店无真实经营场所；运输蔬菜农残异常", at=d(0))

    # 平台移送市场监管，市场监管移送公安；都必须接收或退回
    svc.create_handoff(
        "H001", from_agency="market_regulation", to_agency="public_security",
        due_at=d(48), clue_ref=clue, at=d(0))
    svc.accept_handoff(
        "agency_handoff:H001", agency_id="public_security",
        handled_due_at=d(120), at=d(2))

    # 紧急下架先阻止流通（不替代法定程序）
    svc.restrict_scope(
        "LISTING", listing, agency_id="market_regulation",
        reason="无真实经营场所，平台页面信息疑似虚假", emergency=True,
        clue_ref=clue, at=d(1))

    # 证据链：检查记录与检测结论，终审人不得是提交人
    ev_inspect = svc.submit_evidence(
        "E001", evidence_kind="SITE_INSPECTION", submitted_by="inspector_zhao",
        clue_ref=clue, at=d(3))
    svc.review_evidence(ev_inspect, reviewer="market_regulation",
                        verdict="APPROVED", at=d(4))
    ev_test = svc.submit_evidence(
        "E002", evidence_kind="LAB_REPORT", submitted_by="lab_chen",
        clue_ref=clue, at=d(3))
    svc.review_evidence(ev_test, reviewer="agriculture",
                        verdict="APPROVED", at=d(5))

    # 法定处置：覆盖被紧急下架的页面范围
    action = svc.open_enforcement(
        "A001", agency_id="public_security", basis_refs=[ev_inspect, ev_test],
        clue_ref=clue, scope=("LISTING", listing), at=d(6))
    svc.resolve_enforcement(
        action, agency_id="public_security",
        resolution="查实无证经营，依法作出行政处罚并移送起诉意见", at=d(72))

    # 整改与复查
    rect = svc.request_rectification(
        "R001", agency_id="market_regulation", clue_ref=clue,
        due_at=d(168), at=d(72))
    ev_rect = svc.submit_evidence(
        "E003", evidence_kind="RECTIFICATION_PHOTO", submitted_by="subject_owner",
        clue_ref=clue, at=d(96))
    svc.review_evidence(ev_rect, reviewer="market_regulation",
                        verdict="APPROVED", at=d(100))
    svc.submit_rectification(
        rect, evidence_ref=ev_rect, submitter="subject_owner", at=d(96))
    svc.pass_rectification(rect, reviewer="market_regulation", at=d(100))

    # 复查通过 + 法定处置完成后解除限制
    svc.release_scope(
        "LISTING", listing, reason="行政处罚已执行，整改复查通过", at=d(101))

    return {
        "subject": subject, "premise": premise, "listing": listing,
        "batch": batch, "sample": sample, "clue": clue,
        "ev_inspect": ev_inspect, "ev_test": ev_test, "ev_rect": ev_rect,
        "action": action, "rect": rect,
    }


class FullChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        self.refs = build_full_chain(self.svc)

    def test_closure_gate_and_close(self) -> None:
        clue = self.refs["clue"]
        # 全部闭合条件满足时 blockers 为空，结案成功
        self.assertEqual([], closure_blockers(
            self.svc.state, clue, parse("2026-10-08T00:00:00+08:00")))
        self.svc.close_case(clue, closed_by="market_regulation",
                            at="2026-10-08T09:00:00+08:00")
        status = queries.risk_status(self.svc, clue, "2026-10-08T09:00:00+08:00")
        self.assertTrue(status["chain_complete"])
        self.assertEqual("已结案", status["status_text"])

    def test_one_segment_closed_does_not_close_chain(self) -> None:
        """运输环节单独结案不代表整条风险消除：只解除/办结一个处置时闸口仍拦截。"""
        svc = make_service()
        t = parse("2026-10-01T09:00:00+08:00")
        from datetime import timedelta

        def d(h: int) -> str:
            return (t + timedelta(hours=h)).isoformat()

        svc.declare_duty("market_regulation", "PLATFORM_SUPERVISION", at=d(0))
        svc.declare_duty("agriculture", "CIRCULATION_INSPECTION", at=d(0))
        subject = svc.register_subject(
            "S100", subject_no="91310000MA1PARTIAL", name="半链餐饮",
            profile_fingerprint="fp1", at=d(0))
        listing = svc.register_node("platform_listing", "L100", at=d(0),
                                    listing_name="半链店铺")
        svc.link("PUBLISHED_AS", subject, listing, at=d(0))
        clue = svc.register_clue("C100", source_type="INSPECTION",
                                 subject_ref=subject, at=d(0))
        # 紧急下架做了，但没有法定处置、没有整改：结案必须被拦
        svc.restrict_scope("LISTING", listing, agency_id="market_regulation",
                           reason="线索核查中", emergency=True, clue_ref=clue, at=d(1))
        with self.assertRaises(ClosureBlocked) as ctx:
            svc.close_case(clue, closed_by="market_regulation", at=d(2))
        joined = "；".join(ctx.exception.blockers)
        self.assertIn("紧急", joined)
        self.assertIn("法定程序", joined)

    def test_emergency_restriction_cannot_replace_legal_procedure(self) -> None:
        svc = make_service()
        t = "2026-10-01T09:00:00+08:00"
        svc.declare_duty("market_regulation", "PLATFORM_SUPERVISION", at=t)
        subject = svc.register_subject(
            "S200", subject_no="NO-EMG", name="紧急措施测试店",
            profile_fingerprint="fp", at=t)
        listing = svc.register_node("platform_listing", "L200", at=t,
                                    listing_name="紧急店")
        svc.link("PUBLISHED_AS", subject, listing, at=t)
        clue = svc.register_clue("C200", source_type="INSPECTION",
                                 subject_ref=subject, at=t)
        svc.restrict_scope("LISTING", listing, agency_id="market_regulation",
                           reason="风险", emergency=True, clue_ref=clue, at=t)
        # 没有法定处置结论，不允许解除
        with self.assertRaises(HandoffStateError):
            svc.release_scope("LISTING", listing, reason="想直接解除", at=t)


class JurisdictionTests(unittest.TestCase):
    def test_cannot_act_before_accepting_handoff(self) -> None:
        svc = make_service()
        t = "2026-10-01T09:00:00+08:00"
        svc.declare_duty("market_regulation", "PLATFORM_SUPERVISION", at=t)
        svc.declare_duty("public_security", "CRIMINAL_CLUE", at=t)
        subject = svc.register_subject(
            "S300", subject_no="NO-JUR", name="管辖测试店",
            profile_fingerprint="fp", at=t)
        clue = svc.register_clue("C300", source_type="INSPECTION",
                                 subject_ref=subject, at=t)
        svc.create_handoff("H300", from_agency="market_regulation",
                           to_agency="public_security",
                           due_at="2026-10-03T09:00:00+08:00",
                           clue_ref=clue, at=t)
        ev = svc.submit_evidence("E300", evidence_kind="SITE_INSPECTION",
                                 submitted_by="cop_li", clue_ref=clue, at=t)
        svc.review_evidence(ev, reviewer="public_security", verdict="APPROVED", at=t)
        with self.assertRaises(OutOfJurisdiction):
            svc.open_enforcement("A300", agency_id="public_security",
                                 basis_refs=[ev], clue_ref=clue, at=t)
        # 接收后即可办理
        svc.accept_handoff("agency_handoff:H300", agency_id="public_security",
                           handled_due_at="2026-10-06T09:00:00+08:00", at=t)
        svc.open_enforcement("A300", agency_id="public_security",
                             basis_refs=[ev], clue_ref=clue, at=t)

    def test_handoff_must_be_accepted_or_returned_exactly_once(self) -> None:
        svc = make_service()
        t = "2026-10-01T09:00:00+08:00"
        svc.declare_duty("a", "DUTY_X", at=t)
        svc.declare_duty("b", "DUTY_Y", at=t)
        subject = svc.register_subject("S400", subject_no="N1", name="店",
                                       profile_fingerprint="f", at=t)
        clue = svc.register_clue("C400", source_type="X", subject_ref=subject, at=t)
        svc.create_handoff("H400", from_agency="a", to_agency="b",
                           due_at="2026-10-03T09:00:00+08:00", clue_ref=clue, at=t)
        svc.return_handoff("agency_handoff:H400", agency_id="b",
                           return_reason="非本部门职责", at=t)
        with self.assertRaises(HandoffStateError):
            svc.accept_handoff("agency_handoff:H400", agency_id="b",
                               handled_due_at="2026-10-05T09:00:00+08:00", at=t)
        # 退回后该部门不能据移送行事
        ev = svc.submit_evidence("E400", evidence_kind="K", submitted_by="u",
                                 clue_ref=clue, at=t)
        svc.review_evidence(ev, reviewer="b", verdict="APPROVED", at=t)
        with self.assertRaises(OutOfJurisdiction):
            svc.open_enforcement("A400", agency_id="b", basis_refs=[ev],
                                 clue_ref=clue, at=t)

    def test_agency_without_declared_duty_is_rejected(self) -> None:
        svc = make_service()
        t = "2026-10-01T09:00:00+08:00"
        subject = svc.register_subject("S500", subject_no="N2", name="店",
                                       profile_fingerprint="f", at=t)
        svc.register_clue("C500", source_type="X", subject_ref=subject, at=t)
        with self.assertRaises(OutOfJurisdiction):
            svc.create_handoff("H500", from_agency="ghost_agency",
                               to_agency="market_regulation",
                               due_at="2026-10-03T09:00:00+08:00",
                               clue_ref="risk_clue:C500", at=t)

    def test_duty_code_mismatch_blocks_handoff(self) -> None:
        svc = make_service()
        t = "2026-10-01T09:00:00+08:00"
        svc.declare_duty("a", "DUTY_X", at=t)
        svc.declare_duty("b", "DUTY_Y", at=t)
        subject = svc.register_subject("S550", subject_no="N3", name="店",
                                       profile_fingerprint="f", at=t)
        clue = svc.register_clue("C550", source_type="X", subject_ref=subject, at=t)
        with self.assertRaises(OutOfJurisdiction):
            svc.create_handoff("H550", from_agency="a", to_agency="b",
                               due_at="2026-10-03T09:00:00+08:00",
                               clue_ref=clue, duty_code="DUTY_Z", at=t)


class EvidenceTests(unittest.TestCase):
    def test_submitter_cannot_be_final_reviewer(self) -> None:
        svc = make_service()
        t = "2026-10-01T09:00:00+08:00"
        svc.declare_duty("market_regulation", "DUTY_X", at=t)
        subject = svc.register_subject("S600", subject_no="N4", name="店",
                                       profile_fingerprint="f", at=t)
        svc.register_clue("C600", source_type="X", subject_ref=subject, at=t)
        ev = svc.submit_evidence("E600", evidence_kind="SITE_INSPECTION",
                                 submitted_by="market_regulation", at=t)
        with self.assertRaises(ReviewerConflict):
            svc.review_evidence(ev, reviewer="market_regulation",
                                verdict="APPROVED", at=t)

    def test_enforcement_requires_approved_evidence(self) -> None:
        svc = make_service()
        t = "2026-10-01T09:00:00+08:00"
        svc.declare_duty("market_regulation", "DUTY_X", at=t)
        subject = svc.register_subject("S700", subject_no="N5", name="店",
                                       profile_fingerprint="f", at=t)
        clue = svc.register_clue("C700", source_type="X", subject_ref=subject, at=t)
        ev = svc.submit_evidence("E700", evidence_kind="K", submitted_by="u",
                                 clue_ref=clue, at=t)  # 未终审
        with self.assertRaises(ReviewerConflict):
            svc.open_enforcement("A700", agency_id="market_regulation",
                                 basis_refs=[ev], clue_ref=clue, at=t)

    def test_rectification_reviewer_must_differ_from_submitter(self) -> None:
        svc = make_service()
        t = "2026-10-01T09:00:00+08:00"
        svc.declare_duty("market_regulation", "DUTY_X", at=t)
        subject = svc.register_subject("S800", subject_no="N6", name="店",
                                       profile_fingerprint="f", at=t)
        clue = svc.register_clue("C800", source_type="X", subject_ref=subject, at=t)
        rect = svc.request_rectification(
            "R800", agency_id="market_regulation", clue_ref=clue,
            due_at="2026-10-08T09:00:00+08:00", at=t)
        ev = svc.submit_evidence("E800", evidence_kind="RECTIFICATION",
                                 submitted_by="market_regulation",
                                 clue_ref=clue, at=t)
        svc.submit_rectification(rect, evidence_ref=ev,
                                 submitter="market_regulation", at=t)
        with self.assertRaises(ReviewerConflict):
            svc.pass_rectification(rect, reviewer="market_regulation", at=t)


class CorrectionTests(unittest.TestCase):
    def test_correction_reopens_only_dependent_actions_and_case(self) -> None:
        svc = make_service()
        refs = build_full_chain(svc)
        clue = refs["clue"]
        t0 = "2026-10-08T09:00:00+08:00"
        svc.close_case(clue, closed_by="market_regulation", at=t0)

        # 另有一件不依赖被更正检测结论的处置，保持办结
        other = svc.open_enforcement(
            "A002", agency_id="public_security", basis_refs=[refs["ev_inspect"]],
            clue_ref=clue, scope=("SUBJECT", refs["subject"]), at="2026-10-07T09:00:00+08:00")
        svc.resolve_enforcement(other, agency_id="public_security",
                                resolution="另案处理完毕", at="2026-10-07T18:00:00+08:00")

        # 检测结论更正：E002 被新报告替换
        new_ref = svc.correct_evidence(
            refs["ev_test"], new_evidence_id="E002-V2", submitted_by="lab_chen",
            correction_reason="复检发现样本编号错配，农残实际超标",
            at="2026-10-09T09:00:00+08:00")

        self.assertEqual(graph.EV_SUPERSEDED, svc.state.evidence[refs["ev_test"]].status)
        self.assertEqual(graph.EV_PENDING, svc.state.evidence[new_ref].status)
        # 依赖旧检测结论的处置被重开
        self.assertEqual(graph.A_OPEN, svc.state.enforcements[refs["action"]].status)
        self.assertEqual(1, svc.state.enforcements[refs["action"]].reopen_count)
        # 不依赖它的处置不受影响
        self.assertEqual(graph.A_RESOLVED, svc.state.enforcements[other].status)
        # 已结案的整条风险重新打开
        self.assertEqual(graph.C_OPEN, svc.state.clues[clue].status)
        # 缺证清单应指出新结论尚未终审
        blockers = closure_blockers(
            svc.state, clue, parse("2026-10-09T10:00:00+08:00"))
        self.assertTrue(any("新结论尚未终审" in b for b in blockers))

        # 新结论终审通过后，处置可重新办结，链再次闭合
        svc.review_evidence(new_ref, reviewer="agriculture", verdict="APPROVED",
                            at="2026-10-09T12:00:00+08:00")
        svc.resolve_enforcement(refs["action"], agency_id="public_security",
                                resolution="依更正后检测结论重新作出处罚",
                                at="2026-10-10T09:00:00+08:00")
        svc.close_case(clue, closed_by="market_regulation",
                       at="2026-10-10T10:00:00+08:00")
        self.assertEqual(graph.C_CLOSED, svc.state.clues[clue].status)


class QuarantineTests(unittest.TestCase):
    def test_same_subject_no_changed_fingerprint_is_quarantined(self) -> None:
        svc = make_service()
        t = "2026-10-01T09:00:00+08:00"
        svc.declare_duty("market_regulation", "DUTY_X", at=t)
        subject = svc.register_subject(
            "S900", subject_no="SAME-NO", name="同号不同址店",
            profile_fingerprint="addr-a|lic-1|page-v1", at=t)
        listing = svc.register_node("platform_listing", "L900", at=t,
                                    listing_name="老店")
        svc.link("PUBLISHED_AS", subject, listing, at=t)

        # 平台回调带来同主体号但地址/许可证/页面指纹变化
        svc.revise_subject_profile(
            subject, profile_fingerprint="addr-b|lic-2|page-v2",
            changed_fields=["address", "license_no", "content_fingerprint"], at=t)
        self.assertTrue(svc.state.subjects[subject].quarantined)

        new_listing = svc.register_node("platform_listing", "L901", at=t,
                                        listing_name="换皮新店")
        with self.assertRaises(SubjectQuarantined):
            svc.link("PUBLISHED_AS", subject, new_listing, at=t)

        # 核验被拒：继续隔离
        svc.verify_subject(subject, reviewer="market_regulation",
                           result="REJECTED", at=t)
        self.assertTrue(svc.state.subjects[subject].quarantined)
        with self.assertRaises(SubjectQuarantined):
            svc.link("PUBLISHED_AS", subject, new_listing, at=t)

        # 核验确认后解除隔离
        svc.verify_subject(subject, reviewer="market_regulation",
                           result="CONFIRMED", at=t)
        svc.link("PUBLISHED_AS", subject, new_listing, at=t)
        self.assertFalse(svc.state.subjects[subject].quarantined)


class CallbackReplayTests(unittest.TestCase):
    def test_callback_replay_is_idempotent(self) -> None:
        svc = make_service()
        callback = {
            "event_id": "callback-evt-1",
            "event_type": "SUBJECT_REGISTERED",
            "aggregate_type": "regulated_subject",
            "aggregate_id": "CB1",
            "occurred_at": "2026-10-01T09:00:00+08:00",
            "version": 1,
            "payload": {
                "subject_no": "CB-NO-1",
                "name": "回调店铺",
                "profile_fingerprint": "fp1",
            },
        }
        self.assertTrue(svc.ingest_callback(dict(callback)))
        self.assertFalse(svc.ingest_callback(dict(callback)))  # 重放
        self.assertEqual(1, len(svc.store.all_events()))

    def test_same_id_different_body_is_conflict(self) -> None:
        svc = make_service()
        callback = {
            "event_id": "callback-evt-2",
            "event_type": "SUBJECT_REGISTERED",
            "aggregate_type": "regulated_subject",
            "aggregate_id": "CB2",
            "occurred_at": "2026-10-01T09:00:00+08:00",
            "version": 1,
            "payload": {"subject_no": "N", "name": "原名",
                        "profile_fingerprint": "fp1"},
        }
        svc.ingest_callback(dict(callback))
        tampered = dict(callback, payload=dict(callback["payload"], name="改名"))
        with self.assertRaises(DuplicateConflict):
            svc.ingest_callback(tampered)

    def test_callback_version_must_advance(self) -> None:
        svc = make_service()
        base = {
            "event_id": "cb-v",
            "event_type": "NODE_REGISTERED",
            "aggregate_type": "platform_listing",
            "aggregate_id": "CBV",
            "occurred_at": "2026-10-01T09:00:00+08:00",
            "version": 1,
            "payload": {"node_type": "platform_listing",
                        "node_ref": "platform_listing:CBV"},
        }
        svc.ingest_callback(dict(base))
        bad = dict(base, event_id="cb-v-2", version=3)
        with self.assertRaises(VersionConflict):
            svc.ingest_callback(bad)


class OverdueTests(unittest.TestCase):
    def test_overdue_escalation_uses_original_deadlines_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "events.db")
            svc = make_service(db)
            t = "2026-10-01T09:00:00+08:00"
            svc.declare_duty("a", "DUTY_X", at=t)
            svc.declare_duty("b", "DUTY_Y", at=t)
            subject = svc.register_subject("SOV", subject_no="OV1", name="逾期店",
                                           profile_fingerprint="f", at=t)
            clue = svc.register_clue("COV", source_type="X",
                                     subject_ref=subject, at=t)
            svc.create_handoff("HOV", from_agency="a", to_agency="b",
                               due_at="2026-10-02T09:00:00+08:00",
                               clue_ref=clue, at=t)
            svc.request_rectification("ROV", agency_id="a", clue_ref=clue,
                                      due_at="2026-10-03T09:00:00+08:00", at=t)

            # 故障恢复：新进程从同一事件库重建，期限仍是原始期限
            del svc
            svc = SupervisionService(EventStore(db))
            pending = scheduler.pending_items(svc.state, parse("2026-10-04T09:00:00+08:00"))
            self.assertEqual(2, len(pending))
            self.assertTrue(all(item.overdue for item in pending))

            escalated = scheduler.escalate_overdue(svc, "2026-10-04T09:00:00+08:00")
            self.assertEqual(2, len(escalated))
            # 再次运行（含崩溃后补跑）不重复问责
            again = scheduler.escalate_overdue(svc, "2026-10-05T09:00:00+08:00")
            self.assertEqual([], again)

            # 待接收信息进入卡点查询
            status = queries.risk_status(svc, clue, "2026-10-04T09:00:00+08:00")
            stuck = {(p["stage"], p["agency_id"]) for p in status["stuck_at"]}
            self.assertIn(("PENDING_ACCEPT", "b"), stuck)
            self.assertIn(("PENDING_REVIEW", "a"), stuck)

    def test_not_overdue_before_deadline(self) -> None:
        svc = make_service()
        t = "2026-10-01T09:00:00+08:00"
        svc.declare_duty("a", "DUTY_X", at=t)
        svc.declare_duty("b", "DUTY_Y", at=t)
        subject = svc.register_subject("SOV2", subject_no="OV2", name="未逾期店",
                                       profile_fingerprint="f", at=t)
        clue = svc.register_clue("COV2", source_type="X",
                                 subject_ref=subject, at=t)
        svc.create_handoff("HOV2", from_agency="a", to_agency="b",
                           due_at="2026-10-05T09:00:00+08:00",
                           clue_ref=clue, at=t)
        self.assertEqual([], scheduler.escalate_overdue(svc, "2026-10-02T09:00:00+08:00"))


class ViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        self.refs = build_full_chain(self.svc)

    def test_public_view_hides_reporter_and_case_details(self) -> None:
        view = queries.public_view(self.svc, self.refs["clue"],
                                   "2026-10-02T09:00:00+08:00")
        serialized = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("王某", serialized)
        self.assertNotIn("138", serialized)
        self.assertNotIn("办案", serialized)
        # 仍可看到哪些店铺/商品受限
        self.assertTrue(any(s["in_effect"] for s in view["restricted_scopes"]))

    def test_regulator_view_logs_every_access_and_decision(self) -> None:
        clue = self.refs["clue"]
        queries.regulator_view(self.svc, clue, viewer="auditor_zhou",
                               purpose="督查", at="2026-10-02T09:00:00+08:00")
        queries.regulator_view(self.svc, clue, viewer="auditor_zhou",
                               purpose="再次督查", at="2026-10-03T09:00:00+08:00")
        logs = self.svc.store.access_log("auditor_zhou")
        self.assertEqual(2, len(logs))
        self.assertTrue(all(row["view"] == "REGULATOR" for row in logs))
        view = queries.regulator_view(self.svc, clue, viewer="auditor_zhou",
                                      at="2026-10-04T09:00:00+08:00")
        # 监管视图保留举报人、办案细节与全链要素
        self.assertEqual("王某", view["reporter"])
        self.assertIn("case_detail", view)
        kinds = {e["kind"] for e in view["evidence"]}
        self.assertIn("LAB_REPORT", kinds)

    def test_query_says_what_evidence_is_missing(self) -> None:
        svc = make_service()
        t = "2026-10-01T09:00:00+08:00"
        svc.declare_duty("market_regulation", "DUTY_X", at=t)
        subject = svc.register_subject("SQ", subject_no="Q1", name="缺证店",
                                       profile_fingerprint="f", at=t)
        clue = svc.register_clue("CQ", source_type="X", subject_ref=subject,
                                 case_detail="待查", at=t)
        ev = svc.submit_evidence("EQ", evidence_kind="SITE_INSPECTION",
                                 submitted_by="u", clue_ref=clue, at=t)
        status = queries.risk_status(svc, clue, t)
        self.assertFalse(status["chain_complete"])
        self.assertTrue(any("尚未经终审" in b for b in status["closure_blockers"]))
        self.assertEqual("risk_clue:CQ", status["clue_ref"])
        # 卡点部门查询不泄漏不存在的处置
        svc.review_evidence(ev, reviewer="market_regulation",
                            verdict="REJECTED", reject_reason="材料不清", at=t)
        status = queries.risk_status(svc, clue, t)
        self.assertTrue(any("终审未通过" in b for b in status["closure_blockers"]))


if __name__ == "__main__":
    unittest.main()
