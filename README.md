# 食品安全协同督办图

统一食品安全风险线索、部门职责与处置回执的交换方式，支持链路级督办与范围化限制。

## 目录

- `contracts/domain.schema.json`：事件信封、对象类型和事件载荷约定。
- `data/sample.json`：可直接校验的中文联调样例。
- `src/food_safety_supervision/`：契约校验与命令行入口。
- `tests/`：基础字段、时间版本和事件载荷边界测试。
- `docs/domain.md`：领域对象与事件语义。

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
