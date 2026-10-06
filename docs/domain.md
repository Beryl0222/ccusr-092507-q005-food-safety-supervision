# 领域约定

统一食品安全风险线索、部门职责与处置回执的交换方式，支持链路级督办与范围化限制。

## 图节点（聚合对象）

| aggregate_type | 含义 |
| --- | --- |
| `regulated_subject` | 经营主体（统一主体号 `subject_no` 贯穿全链） |
| `licensed_premise` | 许可场所（许可证号、地址） |
| `platform_listing` | 平台店铺/商品页面（内容指纹） |
| `material_batch` | 原料及流通批次（运输环节蔬菜批次等） |
| `test_sample` | 检测样本与检测结论 |
| `risk_clue` | 风险线索（可匿名） |
| `evidence_record` | 证据记录（线索、检测、回查材料） |
| `legal_duty` | 法定职责（部门 × 职责代码） |
| `agency_handoff` | 跨部门移送（必须被接收或退回，带期限） |
| `enforcement_action` | 处置动作（紧急下架/封存、立案、法定处罚等） |
| `rectification` | 整改与复查（带期限） |

## 事件类型

`NODE_REGISTERED`、`SUBJECT_REGISTERED`、`SUBJECT_PROFILE_REVISED`、`SUBJECT_VERIFIED`、`LINK_ESTABLISHED`、`CLUE_REGISTERED`、
`EVIDENCE_SUBMITTED`、`EVIDENCE_REVIEWED`、`EVIDENCE_CORRECTED`、
`HANDOFF_CREATED`、`HANDOFF_ACCEPTED`、`HANDOFF_RETURNED`、`DUTY_DECLARED`、
`SCOPE_RESTRICTED`、`SCOPE_RELEASED`、`ENFORCEMENT_OPENED`、`ENFORCEMENT_RESOLVED`、
`RECTIFICATION_REQUESTED`、`RECTIFICATION_SUBMITTED`、`RECTIFICATION_PASSED`、
`RECTIFICATION_REJECTED`、`CASE_CLOSED`、`CASE_REOPENED`、`OVERDUE_ESCALATED`。

所有时间都必须携带时区，版本号在同一聚合内从 1 开始严格递增，校验层不会替调用方改写输入。

## 事件载荷

- `NODE_REGISTERED`：`node_type`, `node_ref`；登记主体以外的图节点（许可场所、页面、批次、样本、线索、证据、职责），其余属性放入载荷。
- `SUBJECT_REGISTERED`：`subject_no`, `name`, `profile_fingerprint`；`profile_fingerprint` 由地址、许可证、页面内容指纹等构成。
- `SUBJECT_PROFILE_REVISED`：`subject_no`, `profile_fingerprint`, `changed_fields`；主体号相同但指纹变化时，主体进入隔离状态，核验通过前不得沿既有边继续关联。
- `SUBJECT_VERIFIED`：`reviewer`, `result`（`CONFIRMED`/`REJECTED`）；隔离核验结论，确认后解除隔离。
- `LINK_ESTABLISHED`：`link_type`, `from_ref`, `to_ref`；可附 `valid_from`/`valid_until` 表达生效区间。
- `CLUE_REGISTERED`：`source_type`, `subject_ref`；举报人信息只进入监管视图。
- `EVIDENCE_SUBMITTED`：`evidence_ref`, `submitted_by`, `evidence_kind`。
- `EVIDENCE_REVIEWED`：`evidence_ref`, `reviewer`, `verdict`（`APPROVED`/`REJECTED`）；终审人不得是提交人。
- `EVIDENCE_CORRECTED`：`corrected_ref`, `correction_reason`；只重新打开真正依赖该证据的处置。
- `HANDOFF_CREATED`：`from_agency`, `to_agency`, `due_at`；接收前处于待接收。
- `HANDOFF_ACCEPTED`：`agency_id`, `due_at`。
- `HANDOFF_RETURNED`：`agency_id`, `return_reason`。
- `SCOPE_RESTRICTED`：`scope_type`（`LISTING`/`BATCH`/`PREMISE`/`SUBJECT`）, `scope_ref`；紧急下架与封存先阻止范围流通，不替代法定程序。
- `SCOPE_RELEASED`：`scope_type`, `scope_ref`；限制须经法定处置或复查通过后正式解除，未解除的限制会阻塞全链结案。
- `ENFORCEMENT_OPENED`：`agency_id`, `basis_refs`（所依赖的证据引用列表，检测更正时据此判定重开范围）。
- `ENFORCEMENT_RESOLVED`：`agency_id`, `resolution`（含是否依紧急措施作出）。
- `RECTIFICATION_REQUESTED`：`agency_id`, `due_at`。
- `RECTIFICATION_SUBMITTED`：`evidence_ref`。
- `RECTIFICATION_PASSED` / `RECTIFICATION_REJECTED`：`reviewer`（复查人不得是整改提交人），分别附 `evidence_ref` 或 `reject_reason`。
- `CASE_CLOSED`：`closed_by`；结案闸口要求全链证据终审通过、移送闭合、无未解除限制的残留风险。
- `CASE_REOPENED`：`reason`；检测结论更正等情形自动重开真正依赖该证据的处置，已结案的案件随之重开。
- `OVERDUE_ESCALATED`：`agency_id`, `stage`（`PENDING_ACCEPT`/`PENDING_REVIEW`）, `deadline`。

## 上层服务职责（交换层不管辖）

同一 `event_id` 的重放必须幂等；同号不同内容属冲突，必须拒绝。职责范围校验、移送接收/退回、
回避终审、指纹隔离核验、紧急措施与法定程序分离、更正级联重开、逾期问责与全链结案闸口，
均由协同督办服务在重放事件的关系图投影上执行。
