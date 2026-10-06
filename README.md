# 食品安全协同督办图（后端）

把经营主体、许可场所、平台页面、原料及流通批次、检测样本、风险线索、法定职责、
处置动作和整改复查连成**带生效时间的关系图**；事件溯源持久化，故障恢复后重放即还原，
支持链路级督办、范围化限制、双视图与逾期问责。

## 目录

- `contracts/domain.schema.json`：事件信封、11 类图节点、22 类事件与必填载荷约定。
- `docs/domain.md`：领域对象与事件语义。
- `data/sample.json`：可直接校验的联调样例。
- `src/food_safety_supervision/`：
  - `contracts.py`：交换层契约校验（稳定排序、不改写输入）。
  - `storage.py`：SQLite 仅追加事件库（`event_id` 幂等/同号冲突拒绝）+ 监管访问审计表。
  - `graph.py`：时效关系图投影，纯重放恢复（版本化主体档案、生效边、限制台账、依赖链）。
  - `services.py`：协同督办领域服务，承载全部业务规则与全链结案闸口。
  - `scheduler.py`：按**原始期限**推进待接收/待复查/逾期问责，调度本身无状态、可重放。
  - `queries.py`：卡点部门、受限店铺商品、全链缺证查询；公开视图与监管视图。
  - `errors.py` / `clock.py`：稳定错误码与显式时区时间。
- `tests/`：契约边界测试 + 25 项协同督办端到端规则测试。
- `scripts/demo.py`：幽灵外卖店铺与运输异常蔬菜的全链演示。

## 需求到规则的落点

| 要求 | 实现 |
| --- | --- |
| 对象编号不一致，需要统一关系图 | 统一主体号 `subject_no` 贯穿；`LINK_ESTABLISHED` 建立带 `valid_from/valid_until` 的生效边，图投影沿生效边求连通分量 |
| 各部门只能确认职责范围内事实 | `DUTY_DECLARED` 登记部门职责；未声明职责、职责代码不匹配、移送未接收即行事均被拒（`OutOfJurisdiction`） |
| 跨部门移送必须接收或退回 | `HANDOFF_CREATED/ACCEPTED/RETURNED`，待接收状态阻塞办理，接收与退回互斥且只能一次 |
| 提交人不得为自己证据终审 | `EVIDENCE_REVIEWED` 与整改复查均强制提交人/复查人回避（`ReviewerConflict`） |
| 平台回调可重放 | `event_id` 相同且内容一致 → 幂等跳过；同号内容变化 → `DuplicateConflict`，不覆盖 |
| 主体号相同但地址/许可证/指纹变化 | `SUBJECT_PROFILE_REVISED` 使主体进入隔离，旧关联边暂停使用；`SUBJECT_VERIFIED` 核验后才解除 |
| 紧急下架/封存不替代法定程序 | `SCOPE_RESTRICTED(emergency=true)` 先阻流通；无覆盖该范围的法定处置结论或复查通过，不能 `SCOPE_RELEASED`，也不能结案 |
| 检测更正只重开真正依赖它的处置 | 处置在 `ENFORCEMENT_OPENED.basis_refs` 声明证据依赖；`EVIDENCE_CORRECTED` 仅重开引用旧证据的处置，已结案案件随之重开 |
| 公开视图隐藏举报人/办案细节 | `queries.public_view` 只输出社会可见信息；`regulator_view` 保留全细节且每次访问写审计 |
| 故障恢复按原期限推进 | 期限只存在于事件载荷；恢复即重放；`scheduler.escalate_overdue` 幂等，不重复问责 |
| 查询卡点/受限范围/缺证 | `queries.risk_status` 返回 `stuck_at`、`restricted_scopes`、`closure_blockers` |
| 某环节结案不代表全链消除 | `close_case` 前跑全链闸口 `closure_blockers`：证据终审、移送闭合、法定处置、限制解除、整改复查缺一不可 |

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 端到端演示

```bash
PYTHONPATH=src python3 scripts/demo.py
```

依次展示：结案闸口拦截 → 全链闭合 → 公开视图脱敏 → 检测更正级联重开 →
回调幂等与同号冲突 → 逾期问责幂等 → 监管留痕。

## 契约样例校验

```bash
PYTHONPATH=src python3 -m food_safety_supervision.cli contracts/domain.schema.json data/sample.json
```

命令成功时输出 `valid`；校验失败时逐行输出字段、代码和中文说明，并以非零状态结束。
