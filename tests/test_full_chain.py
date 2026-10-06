"""全链叙事场景测试：幽灵外卖店 + 异常蔬菜的协同督办。

覆盖：编号归并、隔离核验、移送收发、职责边界、提交人回避、
回调幂等重放与指纹隔离、紧急处置/法定追认、检测更正只重开真依赖、
整改复查、逾期问责、崩溃恢复、双视图脱敏与全链闭合。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from food_safety_supervision import (  # noqa: E402
    ContractViolation,
    DuplicateEvent,
    EventStore,
    HandoffClosed,
    IllegalState,
    JurisdictionError,
    ReviewerConflict,
    SupervisionService,
    VersionConflict,
)
from food_safety_supervision import scenario  # noqa: E402
from food_safety_supervision.audit import AuditLog  # noqa: E402
from food_safety_supervision.views import public_view, regulator_view  # noqa: E402

SUBJECT = scenario.SUBJECT
MR = scenario.MARKET_REGULATION
AG = scenario.AGRICULTURE
INSP = scenario.INSPECTION
FSO = scenario.FOOD_SAFETY_OFFICE
CLUE = "clue-C1"


def service(tmp: Path | None = None) -> SupervisionService:
    if tmp is None:
        return scenario.build_service()
    return scenario.build_service(tmp)


def closed_chain(tmp: Path | None = None) -> SupervisionService:
    return scenario.build_closed_chain(tmp)


class FullChainTests(unittest.TestCase):
    def test_closed_chain_has_no_blockers_and_case_can_close(self) -> None:
        svc = closed_chain()
        status = svc.chain_status(CLUE)
        self.assertEqual([], status["blockers"], [b["reason"] for b in status["blockers"]])
        self.assertEqual(status["current_stage"], "已闭合")
        closed = svc.close_case("ev-close", CLUE, FSO, "2026-10-04T09:00:00+08:00")
        self.assertEqual(closed["event_type"], "CASE_CLOSED")
        self.assertTrue(svc.chain_status(CLUE)["closed"])

    def test_restrictions_show_exactly_what_remains_blocked(self) -> None:
        svc = closed_chain()
        status = svc.chain_status(CLUE)
        refs = {r["scope_ref"] for r in status["restricted"]}
        # 批次（运输违法仍成立）、页面（永久下架）、无证场所继续受限；
        # 被替代的紧急封存与原批次处罚已解除，不再出现在受限列表。
        self.assertEqual(refs, {"batch-V2401", "listing-L1", "premise-A2"})
        ids = {r["action_id"] for r in status["restricted"]}
        self.assertNotIn("emg-batch-seal", ids)
        self.assertNotIn("emg-listing-takedown", ids)

    def test_query_says_what_evidence_is_still_missing(self) -> None:
        svc = service()
        svc.register_subject("e1", SUBJECT, "某味餐饮", "2026-09-24T09:00:00+08:00")
        svc.link_batch("e2", "batch-V2401", "异常蔬菜", "2026-09-24T09:20:00+08:00",
                       refs=(SUBJECT,))
        svc.register_clue("e3", CLUE, "inspection", SUBJECT,
                          "2026-09-24T10:00:00+08:00", refs=("batch-V2401",))
        status = svc.chain_status(CLUE)
        self.assertEqual(status["current_department"], AG)
        reasons = [b["reason"] for b in status["blockers"]]
        self.assertTrue(any("农业" in r or "AGRICULTURE" in r or "职责部门" in r
                            for r in reasons), reasons)

    def test_callback_replay_is_idempotent_but_fingerprint_change_isolates(self) -> None:
        svc = service()
        svc.register_subject("e1", SUBJECT, "某味餐饮", "2026-09-24T09:00:00+08:00")
        kw = dict(platform="meituan", subject_no=SUBJECT, fingerprint="fp-v1",
                  observed_at="2026-09-24T08:50:00+08:00",
                  at="2026-09-24T09:10:00+08:00", listing_id="listing-L1")
        svc.record_callback("ev-cb", callback_id="CB-1", **kw)
        svc.record_callback("ev-cb", callback_id="CB-1", **kw)  # 重放
        callbacks = [e for e in svc.store.all_events()
                     if e["event_type"] == "CALLBACK_RECORDED"]
        self.assertEqual(1, len(callbacks))
        self.assertTrue(svc.listing_by_id["listing-L1"].quarantined)
        # 同主体、新指纹：隔离重置。
        svc.record_callback("ev-cb2", callback_id="CB-2",
                            platform="meituan", subject_no=SUBJECT, fingerprint="fp-v2",
                            observed_at="2026-09-25T08:00:00+08:00",
                            at="2026-09-25T08:30:00+08:00", listing_id="listing-L1")
        self.assertTrue(svc.listing_by_id["listing-L1"].quarantined)
        self.assertFalse(svc.listing_by_id["listing-L1"].verified)

    def test_premise_change_quarantines_both_versions_until_verified(self) -> None:
        svc = service()
        svc.register_subject("e1", SUBJECT, "某味餐饮", "2026-09-24T09:00:00+08:00")
        svc.link_premise("e2", "premise-A1", SUBJECT, "地址一", "LIC-1",
                         "2026-01-01T00:00:00+08:00", "2026-09-24T09:00:00+08:00")
        svc.register_clue("e3", CLUE, "patrol", SUBJECT,
                          "2026-09-24T10:00:00+08:00", refs=("premise-A1",))
        svc.link_premise("e4", "premise-A2", SUBJECT, "地址二", "LIC-2",
                         "2026-09-25T00:00:00+08:00", "2026-09-25T09:00:00+08:00")
        self.assertIsNotNone(svc.premises["premise-A1"].valid_to)
        status = svc.chain_status(CLUE)
        self.assertIn("待核验", {b["stage"] for b in status["blockers"]})

    def test_version_conflict_and_duplicate_event_divergence(self) -> None:
        store = EventStore()
        base = {
            "event_id": "x1", "event_type": "SUBJECT_REGISTERED",
            "aggregate_type": "regulated_subject", "aggregate_id": "S",
            "occurred_at": "2026-09-24T09:00:00+08:00", "version": 1,
            "payload": {"subject_no": "S", "name": "甲"},
        }
        store.append(dict(base))
        with self.assertRaises(VersionConflict):
            store.append(dict(base, event_id="x2", version=1))
        with self.assertRaises(DuplicateEvent):
            store.append(dict(base, payload={"subject_no": "S", "name": "被篡改"}))

    def test_cannot_close_before_statutory_and_rectification(self) -> None:
        svc = service()
        svc.register_subject("e1", SUBJECT, "某味餐饮", "2026-09-24T09:00:00+08:00")
        svc.link_premise("e2", "premise-A1", SUBJECT, "虚假地址", "LIC-1",
                         "2026-01-01T00:00:00+08:00", "2026-09-24T09:00:00+08:00")
        svc.register_clue("e3", CLUE, "patrol", SUBJECT,
                          "2026-09-24T10:00:00+08:00", refs=("premise-A1",))
        svc.confirm_fact("e4", CLUE, MR, "premise_address",
                         ("premise-A1",), "2026-09-24T11:00:00+08:00",
                         result="violation")
        svc.restrict_scope("e5", "emg-1", MR, "licensed_premise", "premise-A1",
                           "2026-09-24T11:30:00+08:00", refs=("premise-A1",))
        with self.assertRaises(IllegalState) as ctx:
            svc.close_case("e6", CLUE, FSO, "2026-09-24T12:00:00+08:00")
        self.assertIn("法定", str(ctx.exception))

    def test_emergency_action_cannot_replace_statutory_procedure(self) -> None:
        svc = service()
        svc.register_subject("e1", SUBJECT, "某味餐饮", "2026-09-24T09:00:00+08:00")
        svc.link_premise("e2", "premise-A1", SUBJECT, "虚假地址", "LIC-1",
                         "2026-01-01T00:00:00+08:00", "2026-09-24T09:00:00+08:00")
        svc.register_clue("e3", CLUE, "patrol", SUBJECT,
                          "2026-09-24T10:00:00+08:00", refs=("premise-A1",))
        svc.confirm_fact("e4", CLUE, MR, "premise_address", ("premise-A1",),
                         "2026-09-24T11:00:00+08:00", result="violation")
        svc.restrict_scope("e5", "emg-1", MR, "licensed_premise", "premise-A1",
                           "2026-09-24T11:30:00+08:00", refs=("premise-A1",))
        stages = {b["stage"] for b in svc.chain_status(CLUE)["blockers"]}
        self.assertIn("待法定追认", stages)
        self.assertIn("待法定处置", stages)

    def test_crash_recovery_replays_and_partial_tail_is_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            svc = closed_chain(tmp)
            status_before = svc.chain_status(CLUE)
            event_count = len(svc.store.all_events())

            # 模拟崩溃：事件日志与审计日志末尾各出现写了一半的残行。
            with (tmp / "events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write('{"event_id": "torn", "event_type": "SUBJ')
            with (tmp / "audit.jsonl").open("a", encoding="utf-8") as handle:
                handle.write('{"at": "2026-10-04T')

            recovered = scenario.build_service(tmp)
            self.assertEqual(event_count, len(recovered.store.all_events()))
            status_after = recovered.chain_status(CLUE)
            self.assertEqual(
                [b["reason"] for b in status_before["blockers"]],
                [b["reason"] for b in status_after["blockers"]],
            )
            # 恢复后重放原命令：幂等，不产生重复事件或重复决定审计。
            decisions_before = len(recovered.audit.entries("decision"))
            recovered.register_subject(
                "ev-subject", SUBJECT, "某味餐饮管理有限公司",
                "2026-09-24T09:00:00+08:00",
                aliases=("MR-PLATFORM-7781", "PS-CLUE-33"),
            )
            self.assertEqual(event_count, len(recovered.store.all_events()))
            self.assertEqual(decisions_before, len(recovered.audit.entries("decision")))
            # 期限推进不丢：结案事件可正常追加。
            recovered.close_case("ev-close", CLUE, FSO, "2026-10-04T09:00:00+08:00")

    def test_overdue_history_is_kept_after_resolution(self) -> None:
        svc = closed_chain()
        handoff_overdue = svc.overdue[("agency_handoff", "handoff-H1")]
        self.assertIsNotNone(handoff_overdue["flagged_at"])
        self.assertIsNotNone(handoff_overdue["resolved_at"])
        rect_overdue = svc.overdue[("rectification", "rectification-R1")]
        self.assertIsNotNone(rect_overdue["resolved_at"])
        self.assertEqual([], svc.scan_overdue("2026-10-05T09:00:00+08:00"))

    def test_public_view_hides_reporter_and_case_details(self) -> None:
        svc = closed_chain()
        svc.close_case("ev-close", CLUE, FSO, "2026-10-04T09:00:00+08:00")
        pub = public_view(svc, CLUE)
        raw = json.dumps(pub, ensure_ascii=False)
        for secret in ("张三", "138****0000", "PUBLIC_SECURITY",
                       "investigation_clue", "公安"):
            self.assertNotIn(secret, raw)
        self.assertIn("restricted_goods_and_shops", pub)
        self.assertEqual("某味餐饮管理有限公司", pub["subject_name"])

    def test_regulator_view_keeps_details_and_logs_every_access(self) -> None:
        svc = closed_chain()
        view = regulator_view(svc, CLUE, "auditor-李")
        self.assertEqual("张三", view["reporter"].get("reporter_name"))
        self.assertIn(("agency_handoff", "handoff-H1"),
                      {(o["ref_type"], o["ref_id"]) for o in view["overdue"]})
        accesses = [e for e in svc.audit.entries("access")
                    if e["action"] == "view_regulator_chain"]
        self.assertEqual("auditor-李", accesses[-1]["actor"])
        self.assertEqual(CLUE, accesses[-1]["target"])
        decisions = {e["action"] for e in svc.audit.entries("decision")}
        for expected in ("accept_handoff", "reject_handoff", "confirm_fact",
                         "finalize_evidence", "emergency_restrict", "statutory_action",
                         "correct_test", "review_rectification", "flag_overdue"):
            self.assertIn(expected, decisions)

    def test_contract_rejects_payload_without_timezone(self) -> None:
        svc = service()
        svc.register_subject("e0", SUBJECT, "某味餐饮", "2026-09-24T09:00:00+08:00")
        with self.assertRaises(ContractViolation):
            svc.link_premise(
                "bad-ts", "premise-X", SUBJECT, "地址", "LIC",
                "2026-01-01T00:00:00", "2026-09-24T09:00:00",
            )


if __name__ == "__main__":
    unittest.main()
