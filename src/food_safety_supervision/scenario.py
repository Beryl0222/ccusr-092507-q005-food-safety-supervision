"""联调叙事场景：幽灵外卖店 + 运输环节异常蔬菜。

本模块用确定的事件标识与时间线推进一条完整风险链，供命令行演示和
测试复用。脚本中穿插的越权、自审、无理由退回等调用均被领域规则拒绝，
属于场景的一部分。
"""

from __future__ import annotations

from pathlib import Path

from .audit import AuditLog
from .errors import (
    ContractViolation,
    HandoffClosed,
    JurisdictionError,
    ReviewerConflict,
)
from .events import EventStore
from .service import SupervisionService

SUBJECT = "USCC-91310000MA1FX0XX01"
MARKET_REGULATION = "MARKET_REGULATION"
AGRICULTURE = "AGRICULTURE"
INSPECTION = "INSPECTION"
PUBLIC_SECURITY = "PUBLIC_SECURITY"
FOOD_SAFETY_OFFICE = "FOOD_SAFETY_OFFICE"


class expect:  # noqa: N801 - 场景脚本里读作“预期抛错”
    def __init__(self, exc_type: type) -> None:
        self.exc_type = exc_type

    def __enter__(self) -> "expect":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is None or not issubclass(exc_type, self.exc_type):
            raise AssertionError(f"预期 {self.exc_type.__name__}，实际: {exc!r}")
        return True


def build_service(tmp: str | Path | None = None) -> SupervisionService:
    if tmp is None:
        return SupervisionService(EventStore(), AuditLog())
    path = Path(tmp)
    return SupervisionService(
        EventStore(path / "events.jsonl"), AuditLog(path / "audit.jsonl")
    )


def build_closed_chain(tmp: str | Path | None = None) -> SupervisionService:
    """按时间线推进到全链闭合（含逾期、退回、更正重开等波折）。"""
    svc = build_service(tmp)
    mr, ag, insp, ps, fso = (
        MARKET_REGULATION, AGRICULTURE, INSPECTION, PUBLIC_SECURITY, FOOD_SAFETY_OFFICE,
    )
    subject = SUBJECT

    # 9月24日：市场监管与平台分别发现“某味餐饮”无真实经营场所。
    svc.register_subject(
        "ev-subject", subject, "某味餐饮管理有限公司",
        "2026-09-24T09:00:00+08:00",
        aliases=("MR-PLATFORM-7781", "PS-CLUE-33"),
    )
    svc.link_premise(
        "ev-premise-1", "premise-A1", subject, "登记地址：某写字楼302（实际不存在）",
        "LIC-SH-0001", "2026-01-01T00:00:00+08:00",
        "2026-09-24T09:05:00+08:00",
    )
    for _ in range(2):  # 同一平台回调重放，必须幂等。
        svc.record_callback(
            "ev-callback-1", "meituan", "CB-20260924-01", subject,
            "fp-v1", "2026-09-24T08:50:00+08:00", "2026-09-24T09:10:00+08:00",
            url="https://example/shop/7781", listing_id="listing-L1",
        )
    svc.link_batch(
        "ev-batch", "batch-V2401", "青菜（运输温控异常批次）",
        "2026-09-24T09:20:00+08:00", refs=(subject,),
    )
    svc.link_sample(
        "ev-sample", "sample-S1", "batch-V2401", "2026-09-24T09:30:00+08:00",
    )
    svc.register_clue(
        "ev-clue", "clue-C1", "public_report", subject,
        "2026-09-24T10:00:00+08:00",
        refs=("premise-A1", "listing-L1", "batch-V2401"),
        reporter_name="张三", reporter_contact="138****0000",
    )

    # 公安只能确认侦查事实；越权确认地址事实被拒绝。
    svc.confirm_fact(
        "ev-fact-ps", "clue-C1", ps, "investigation_clue",
        (subject,), "2026-09-24T10:20:00+08:00",
    )
    with expect(JurisdictionError):
        svc.confirm_fact(
            "ev-fact-cross", "clue-C1", ps, "premise_address",
            ("premise-A1",), "2026-09-24T10:21:00+08:00",
        )

    # 紧急下架与封存先阻止流通。
    svc.restrict_scope(
        "ev-emg-listing", "emg-listing-takedown", mr,
        "platform_listing", "listing-L1", "2026-09-24T10:30:00+08:00",
        refs=("listing-L1",), action_type="平台紧急下架",
    )
    svc.restrict_scope(
        "ev-emg-batch", "emg-batch-seal", ag,
        "material_batch", "batch-V2401", "2026-09-24T10:31:00+08:00",
        based_on=("sample-S1",), action_type="蔬菜先行登记保存（封存）",
    )

    # 9月25日：页面换壳（指纹变化）、场所改报地址或许可证变化，一律隔离核验。
    svc.record_callback(
        "ev-callback-2", "meituan", "CB-20260925-02", subject,
        "fp-v2", "2026-09-25T08:00:00+08:00", "2026-09-25T08:30:00+08:00",
        url="https://example/shop/7781", listing_id="listing-L1",
    )
    svc.link_premise(
        "ev-premise-2", "premise-A2", subject, "改报地址：某小区车库（无证）",
        "LIC-SH-0001-NEW", "2026-09-25T00:00:00+08:00",
        "2026-09-25T09:00:00+08:00",
    )
    svc.confirm_fact(
        "ev-fact-p1", "clue-C1", mr, "premise_address",
        ("premise-A1",), "2026-09-25T10:00:00+08:00", result="violation",
    )
    svc.confirm_fact(
        "ev-fact-p2", "clue-C1", mr, "license_status",
        ("premise-A2",), "2026-09-25T10:05:00+08:00", result="violation",
    )
    svc.confirm_fact(
        "ev-fact-l", "clue-C1", mr, "listing_identity_verified",
        ("listing-L1",), "2026-09-25T10:10:00+08:00", result="violation",
    )

    # 移送：逾期被问责 → 无理由退回被拒 → 附理由退回 → 重新移送并接收。
    svc.open_handoff(
        "ev-handoff-1", "handoff-H1", fso, ag,
        "2026-09-25T12:00:00+08:00", ("batch-V2401",),
        "2026-09-25T11:00:00+08:00",
    )
    svc.scan_overdue("2026-09-26T09:00:00+08:00")
    with expect(JurisdictionError):
        svc.decide_handoff(
            "ev-h1-other", "handoff-H1", "OTHER_AGENCY", True,
            "2026-09-26T10:00:00+08:00",
        )
    with expect(ContractViolation):
        svc.decide_handoff(
            "ev-h1-badreject", "handoff-H1", ag, False,
            "2026-09-26T10:05:00+08:00",
        )
    svc.decide_handoff(
        "ev-h1-reject", "handoff-H1", ag, False,
        "2026-09-26T10:10:00+08:00", reason="证据材料不全，退回补正",
    )
    svc.open_handoff(
        "ev-handoff-2", "handoff-H2", fso, ag,
        "2026-09-27T12:00:00+08:00", ("batch-V2401",),
        "2026-09-26T11:00:00+08:00",
    )
    svc.decide_handoff(
        "ev-h2-accept", "handoff-H2", ag, True,
        "2026-09-26T11:30:00+08:00",
    )
    with expect(HandoffClosed):
        svc.decide_handoff(
            "ev-h2-again", "handoff-H2", ag, True,
            "2026-09-26T11:31:00+08:00",
        )
    svc.confirm_fact(
        "ev-fact-b1", "clue-C1", ag, "transport_condition",
        ("batch-V2401",), "2026-09-27T09:00:00+08:00", result="violation",
    )

    # 检测阳性 → 证据提交人自审被拒 → 他人终审通过。
    svc.conclude_test(
        "ev-test-positive", "sample-S1", insp, "不合格（农残超标）",
        "2026-09-27T15:00:00+08:00",
    )
    svc.submit_evidence(
        "ev-evidence", "evidence-E1", "inspector-钱七",
        ("batch-V2401", "sample-S1"), "2026-09-27T15:30:00+08:00",
    )
    with expect(ReviewerConflict):
        svc.finalize_evidence(
            "ev-final-bad", "evidence-E1", "inspector-钱七", "approved",
            "2026-09-27T16:00:00+08:00",
        )
    svc.finalize_evidence(
        "ev-final-ok", "evidence-E1", "MR-officer-王五", "approved",
        "2026-09-27T17:00:00+08:00",
    )

    # 法定程序落地并替代紧急措施。
    svc.take_statutory_action(
        "ev-stat-premise", "stat-premise-revoke", mr,
        "查封无证场所并撤销许可备案", "食品安全法 第一百二十二条",
        "2026-09-28T09:00:00+08:00",
        refs=("premise-A1", "premise-A2"), scope_type="licensed_premise",
        scope_ref="premise-A2", rectification_required=False, restrictive=True,
    )
    svc.take_statutory_action(
        "ev-stat-listing", "stat-listing-order", mr,
        "责令平台永久下架并处罚", "食品安全法 第一百三十一条",
        "2026-09-28T09:30:00+08:00",
        refs=("listing-L1",), lifts=("emg-listing-takedown",),
        scope_type="platform_listing", scope_ref="listing-L1", restrictive=True,
    )
    svc.take_statutory_action(
        "ev-stat-batch", "stat-batch-penalty", ag,
        "不合格农产品查封、召回并处罚", "农产品质量安全法 第六十条",
        "2026-09-28T10:00:00+08:00",
        refs=("batch-V2401",), based_on=("sample-S1",),
        lifts=("emg-batch-seal",),
        scope_type="material_batch", scope_ref="batch-V2401", restrictive=True,
    )

    # 9月29日检测更正：只重开真正依赖该样本且仍生效的处置。
    svc.correct_test(
        "ev-test-correct", "sample-S1", insp, "合格（复检）",
        "实验室前处理污染导致假阳性", "2026-09-29T11:00:00+08:00",
    )
    # 运输温控违法独立成立：重新作出法定决定。
    svc.take_statutory_action(
        "ev-stat-batch-2", "stat-batch-transport", ag,
        "按运输温控违法重新查封、召回并责令整改", "农产品质量安全法 第五十四条",
        "2026-09-30T09:00:00+08:00",
        refs=("batch-V2401",), lifts=("stat-batch-penalty",),
        scope_type="material_batch", scope_ref="batch-V2401",
        rectification_required=False, restrictive=True,
    )

    # 整改：复查逾期被问责，提交人自查被拒，市场监管复查通过。
    svc.submit_rectification(
        "ev-rect", "rectification-R1", "某味餐饮-代理人",
        ("stat-listing-order",), "2026-10-01T09:00:00+08:00",
        review_due_at="2026-10-02T09:00:00+08:00",
    )
    svc.scan_overdue("2026-10-03T09:00:00+08:00")
    with expect(ReviewerConflict):
        svc.review_rectification(
            "ev-rect-self", "rectification-R1", "某味餐饮-代理人", True,
            "2026-10-03T10:00:00+08:00",
        )
    svc.review_rectification(
        "ev-rect-ok", "rectification-R1", mr, True,
        "2026-10-03T11:00:00+08:00",
    )
    return svc
