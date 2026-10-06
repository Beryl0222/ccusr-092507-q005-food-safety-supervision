"""命令行演示：按时间线打印协同督办链路的关键时点。

用法::

    PYTHONPATH=src python3 -m food_safety_supervision.demo
"""

from __future__ import annotations

import json

from . import scenario
from .views import public_view, regulator_view

CLUE = "clue-C1"


def _print_status(title: str, svc, at: str) -> None:
    status = svc.chain_status(CLUE)
    print(f"\n=== {title}（{at}）===")
    if status["blockers"]:
        print(f"卡点部门：{status['current_department']} · 阶段：{status['current_stage']}")
        for blocker in status["blockers"]:
            print(f"  - [{blocker['stage']}/{blocker['agency']}] {blocker['reason']}")
    else:
        print("卡点部门：无（全链已闭合）")
    restricted = status["restricted"]
    if restricted:
        print("受限范围：")
        for item in restricted:
            print(f"  - {item['action_type']} → {item['scope_type']}:{item['scope_ref']}"
                  f"（{item['kind']}，{item['status']}）")
    else:
        print("受限范围：无")


def main() -> int:
    # 时点一：只有线索与紧急措施，现场、移送、检测都还没到位。
    svc = scenario.build_service()
    subject = scenario.SUBJECT
    svc.register_subject(
        "ev-subject", subject, "某味餐饮管理有限公司", "2026-09-24T09:00:00+08:00",
        aliases=("MR-PLATFORM-7781", "PS-CLUE-33"),
    )
    svc.link_premise(
        "ev-premise-1", "premise-A1", subject, "登记地址：某写字楼302（实际不存在）",
        "LIC-SH-0001", "2026-01-01T00:00:00+08:00", "2026-09-24T09:05:00+08:00",
    )
    svc.record_callback(
        "ev-callback-1", "meituan", "CB-20260924-01", subject,
        "fp-v1", "2026-09-24T08:50:00+08:00", "2026-09-24T09:10:00+08:00",
        url="https://example/shop/7781", listing_id="listing-L1",
    )
    svc.link_batch("ev-batch", "batch-V2401", "青菜（运输温控异常批次）",
                   "2026-09-24T09:20:00+08:00", refs=(subject,))
    svc.link_sample("ev-sample", "sample-S1", "batch-V2401", "2026-09-24T09:30:00+08:00")
    svc.register_clue(
        "ev-clue", CLUE, "public_report", subject, "2026-09-24T10:00:00+08:00",
        refs=("premise-A1", "listing-L1", "batch-V2401"),
        reporter_name="张三", reporter_contact="138****0000",
    )
    svc.restrict_scope(
        "ev-emg-listing", "emg-listing-takedown", scenario.MARKET_REGULATION,
        "platform_listing", "listing-L1", "2026-09-24T10:30:00+08:00",
        refs=("listing-L1",), action_type="平台紧急下架",
    )
    svc.restrict_scope(
        "ev-emg-batch", "emg-batch-seal", scenario.AGRICULTURE,
        "material_batch", "batch-V2401", "2026-09-24T10:31:00+08:00",
        based_on=("sample-S1",), action_type="蔬菜先行登记保存（封存）",
    )
    _print_status("发现风险：紧急措施已落地，但法定程序尚未开始", svc, "2026-09-24 10:31")

    pub = public_view(svc, CLUE)
    print("\n--- 公开视图（脱敏）---")
    print(json.dumps(pub, ensure_ascii=False, indent=2))

    # 时点二：完整时间线推进到闭合。
    svc = scenario.build_closed_chain()
    _print_status("全链推进：经退回、更正重开、整改复查后", svc, "2026-10-03 11:00")

    reg = regulator_view(svc, CLUE, "督查员-周")
    print("\n--- 监管视图（完整事实，本次访问已入审计）---")
    print(f"举报人：{reg['reporter'].get('reporter_name')} / "
          f"{reg['reporter'].get('reporter_contact')}")
    print(f"事实确认：{[(f['fact_type'], f['agency_id']) for f in reg['facts']]}")
    print(f"样本结论演变：{[(s['sample_id'], [h['conclusion'] for h in s['history']]) for s in reg['samples']]}")
    print(f"审计访问记录：{len(svc.audit.entries('access'))} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
