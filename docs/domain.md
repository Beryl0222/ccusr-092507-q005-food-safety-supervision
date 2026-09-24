# 领域约定

统一食品安全风险线索、部门职责与处置回执的交换方式，支持链路级督办与范围化限制。

聚合对象包括`regulated_subject`、`risk_clue`、`agency_handoff`、`enforcement_action`。事件类型包括`CLUE_REGISTERED`、`HANDOFF_ACCEPTED`、`SCOPE_RESTRICTED`、`EVIDENCE_CORRECTED`、`CASE_CLOSED`。所有时间都必须携带时区，版本号从 1 开始递增，校验层不会替调用方改写输入。

## 事件载荷

- `CLUE_REGISTERED`：还需包含 `source_type`, `subject_ref`。
- `HANDOFF_ACCEPTED`：还需包含 `agency_id`, `due_at`。
- `SCOPE_RESTRICTED`：还需包含 `scope_type`, `scope_ref`。

同一事件标识的幂等与冲突处理属于上层业务服务职责；交换层只负责稳定报告结构、枚举、时间、版本和必需载荷问题。
