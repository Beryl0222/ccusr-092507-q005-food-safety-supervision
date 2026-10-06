# 食品安全协同督办图后端

把经营主体、许可场所、平台页面、原料及流通批次、检测样本、风险线索、法定职责、
处置动作和整改复查连成带生效时间的关系图，支持跨部门协同督办与全链闭合判定。

## 需求落点

| 需求 | 实现 |
| --- | --- |
| 各部门只能确认职责范围内的事实 | `confirm_fact` 按 `DEFAULT_MANDATES` 校验事实归口，越权抛 `JurisdictionError` |
| 跨部门移送必须明确接收或退回 | `HANDOFF_OPENED/ACCEPTED/REJECTED`，退回强制理由，退回后需重新移送 |
| 提交人不得为自己的证据作终审 | `finalize_evidence` / `review_rectification` 对比提交人，命中抛 `ReviewerConflict` |
| 同一平台回调可重放 | `EventStore` 按 `event_id` 幂等（剔除版本号比对内容），审计不重复 |
| 主体号相同但指纹/地址/许可证变化要隔离核验 | 页面指纹变化重置隔离；场所变化旧版到期、新版隔离；现场认定违法后进入法定轨道 |
| 紧急下架与封存先阻止流通但不替代法定程序 | 紧急动作有 `statutory_successor` 或被 `lifts` 解除；闭合检查要求法定覆盖与追认 |
| 检测结论更正只重新打开真正依赖它的处置 | `correct_test` 仅对 `based_on` 该样本且仍生效的动作发 `ACTION_REOPENED` |
| 公开视图隐藏举报人与办案细节 | `views.public_view` 脱敏且不记访问审计；监管视图全量并记每次访问 |
| 故障恢复后按原期限推进 | JSONL + fsync，残尾截断后重放重建；`scan_overdue` 确定性问责 |
| 查询说明卡点部门、受限范围、缺失证据 | `chain_status` 返回 `current_department`、`restricted`、`blockers` |
| 一环节结案不等于全链消除 | 所有闭合条件满足前 `CASE_CLOSED` 被 `IllegalState` 拒绝 |

## 目录

- `contracts/domain.schema.json`：事件信封、九类聚合、22 种事件及载荷约定。
- `data/sample.json`：可直接校验的中文联调样例。
- `src/food_safety_supervision/`
  - `contracts.py`：契约校验（含载荷内时间字段、引用列表）。
  - `events.py`：幂等 JSONL 事件存储，版本冲突检测，崩溃残尾截断重放。
  - `audit.py`：决定/访问双类审计台账。
  - `errors.py`：稳定错误码（职责、回避、移送已决、版本冲突等）。
  - `service.py`：协同督办领域服务（折叠 + 命令 + 闭合判定）。
  - `views.py`：公开/监管双视图。
  - `scenario.py`：确定时间线的全链联调场景。
  - `demo.py`：命令行演示，展示两个关键时点的查询结果。
- `tests/`：契约边界测试与 13 个全链叙事测试。
- `docs/domain.md`：领域语义与业务规则。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 样例校验

```bash
PYTHONPATH=src python3 -m food_safety_supervision.cli contracts/domain.schema.json data/sample.json
```

命令成功时输出 `valid`；校验失败时逐行输出字段、代码和中文说明，并以非零状态结束。

## 演示

```bash
PYTHONPATH=src python3 -m food_safety_supervision.demo
```

依次展示：发现风险时（紧急措施落地但多处缺证据）的卡点部门与受限范围、
公开视图脱敏效果，以及全链推进（移送退回、检测更正重开、整改复查）后
无阻碍、仅剩法定限制的闭合状态；最后打印监管视图中的举报人保护前后对比、
事实归口与检测结论演变。
