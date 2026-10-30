# 阶段 37：模型精度评测数据契约与持久化

## 阶段边界

本阶段只增加模型精度评测的 Pydantic 数据契约、SQLite 表和 Store。不会启动 EvalScope，
不会连接模型服务，不新增 HTTP API，也不显示前端入口。

数据库 schema 从 20 升至 21，新增：

- `accuracy_runs`：目标快照、请求、解析配置、任务状态、进度、EvalScope 身份、错误和时间。
- `accuracy_run_metrics`：按数据集、评测版本、指标、类别和子集保存归一化结果。

指标表通过外键 `ON DELETE CASCADE` 归属于运行记录。回滚应用代码时保留新增表，不删除用户数据库。

## 自动验收

```bash
./scripts/accept-stage-37.sh
```

验收覆盖：

- 新数据库创建两张表和五个查询索引。
- schema 20 数据库原地升级到 21，已有部署记录不丢失。
- 创建、读取、分页列表和 JSON 契约往返。
- `queued → preparing → running → parsing → completed/partial` 状态转换。
- 非法跨阶段转换和进度阶段不一致被拒绝。
- 完成、部分完成、失败、取消和错误详情持久化。
- 重启恢复将 queued/preparing/running/parsing 标记为 interrupted。
- 删除运行记录后归一化指标通过外键级联删除。
- Pydantic 拒绝未知字段、重复数据集、越界样本数和不一致计数。

## 本次执行结果

- 执行日期：2026-10-22。
- 阶段 37 专项测试：`9 passed`。
- 全量回归：`164 passed in 119.40s`；现有部署恢复专项测试包含在内。
- 全项目 Ruff：通过。
- `src` 与 `tests` compileall：通过。
- 临时 SQLite `quick_check`：`ok`。
- 实际结构：两张新表、五个新索引、最新 schema version 21。
- `accuracy_run_metrics.run_id` 外键删除策略：`CASCADE`。
- 验收脚本未访问网络、未连接模型、未启动 EvalScope。

全量回归前发现虚拟环境依赖版本偏离 `uv.lock`，已按锁文件恢复；项目自带的固定 Node.js
也已加入测试进程路径。依赖同步本身访问了 Python 包仓库，但阶段 37 验收和全量测试过程没有
发起模型或数据集网络请求。

## 人工验收

使用临时数据库执行以下只读结构检查：

```bash
sqlite3 <临时数据库> '.schema accuracy_runs'
sqlite3 <临时数据库> '.schema accuracy_run_metrics'
sqlite3 <临时数据库> 'PRAGMA foreign_key_list(accuracy_run_metrics);'
sqlite3 <临时数据库> 'SELECT version FROM schema_migrations ORDER BY version;'
```

预期最新 schema version 为 21，指标外键删除策略为 `CASCADE`。

## 通过标准

- 专项 Pytest、Ruff、compileall 和独立验收脚本全部通过。
- 阶段 35 数据库可原地升级且已有记录保留。
- 原有全量测试无新增失败。
- 测试过程不访问网络、不调用模型、不启动 EvalScope。
- `main.py`、前端和 HTTP 路由没有变化。

## 回滚

可以回滚本阶段 Python 代码和 schema version 常量。已经创建的 `accuracy_runs` 与
`accuracy_run_metrics` 表应保留，旧代码不会读取它们；不得通过删除数据库或删除表来回滚。

## 下一阶段边界

阶段 38 才会增加 EvalScope 适配器、共享目标解析器、后台任务、取消、进度采集和报告归一化。
必须在用户确认阶段 37 后才能开始。
