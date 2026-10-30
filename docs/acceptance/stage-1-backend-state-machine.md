# 阶段 1 验收报告：后端、SQLite 与任务状态机

- 日期：2026-08-31
- 状态：实现与开发侧测试完成，等待用户验收
- 本阶段没有下载模型、启动 vLLM 容器或实现 Web UI。

## 本阶段交付

- FastAPI 本地 API 服务及自动 OpenAPI 文档。
- SQLite 初始化、版本记录、WAL 模式和完整性检查。
- 部署任务创建、列表、详情、取消接口。
- 每个任务独立递增的事件时间线。
- 显式状态迁移规则，拒绝跳过阶段的非法迁移。
- 进程中断后的未完成任务恢复：启动时重新排队并记录恢复事件。
- 终态任务跨 API 重启持久化。
- 输入校验、404 和 409 错误语义。
- 固定版本范围和 `uv.lock`，保证环境可复现。

## API 范围

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/v1/health` | API、SQLite 完整性和恢复数量 |
| POST | `/api/v1/deployments` | 创建排队中的部署任务 |
| GET | `/api/v1/deployments` | 分页查询部署任务 |
| GET | `/api/v1/deployments/{id}` | 查询单个任务 |
| POST | `/api/v1/deployments/{id}/cancel` | 取消任务，重复取消保持幂等 |
| GET | `/api/v1/deployments/{id}/events` | 查询按顺序保存的状态事件 |

阶段 1 没有执行器，因此新任务会保持 `queued`。这是有意行为，不模拟虚假的下载或部署进度。

## 状态模型

```text
queued → preflight → downloading_model → pulling_image
→ starting_container → loading_model → health_checking → running
→ stopping → stopped
```

执行过程可进入 `failed` 或 `cancelled`。`stopped`、`failed` 和 `cancelled` 可在后续阶段通过重试重新进入 `queued`。阶段 1 只公开创建、读取和取消接口；真实状态推进将在阶段 2/3 的执行器中使用。

## 开发侧测试结果

```text
6 passed in 0.14s
```

覆盖内容：

- API 和 SQLite 健康检查。
- 创建、列表、详情、取消、重复取消及事件顺序。
- 非法模型 ID 返回 422，不存在的任务返回 404。
- 状态机拒绝从 `queued` 直接跳到 `running`。
- 模拟在 `preflight` 中断，重启后恢复为 `queued`、attempt 增加并记录 `recovered` 事件。
- 已取消任务在应用重启后保持原状态。
- 源码和测试文件可通过 Python 字节码编译。

真实进程验收脚本也已经由开发侧运行通过，包括启动 Uvicorn、调用 HTTP API、停止并重启 Uvicorn、复查 SQLite 数据。

## 用户自动验收

在项目目录运行：

```bash
./scripts/accept-stage-1.sh
```

脚本使用 `/tmp` 下的隔离数据库，不修改正式 `data/`，会执行：

1. 全部自动化测试。
2. 启动真实 Uvicorn API。
3. 检查 API 和 SQLite 健康状态。
4. 创建、读取和列出一个部署任务。
5. 取消任务并验证事件时间线。
6. 重启 API 进程。
7. 验证任务仍存在且状态正确。
8. 停止 API 并清理隔离测试数据。

预期最后一行：

```text
PASS: stage 1 acceptance checks succeeded.
```

任何 `FAIL:`、HTTP 错误、测试失败或非零退出码均表示验收不通过。

## 用户人工验收

启动一个保留数据的本地服务：

```bash
UNIFORM_DATA_DIR=./data uv run uvicorn uniform_dgxspark.main:app \
  --host 127.0.0.1 --port 18180
```

浏览器打开：

```text
http://127.0.0.1:18180/docs
```

在 Swagger 页面按顺序执行：

1. `GET /api/v1/health`，预期 `status` 和 `database` 都是 `ok`。
2. `POST /api/v1/deployments`，请求体使用 `{"model_id":"Qwen/Qwen2.5-0.5B-Instruct"}`，预期 HTTP 201、状态为 `queued`。
3. 复制响应中的 `id`，调用详情和事件接口，预期分别找到任务和一个 `created` 事件。
4. 调用取消接口，预期状态变成 `cancelled`。
5. 再次查询事件，预期依次出现 `created`、`cancelled`。
6. 在终端按 `Ctrl+C`，重新运行启动命令，再查询原 ID，预期任务仍存在且为 `cancelled`。

人工测试产生的 `./data/uniform.db` 是本地测试数据。如需清理，应先停止 API，再删除整个 `./data` 目录；自动验收脚本不会创建该目录。

## 已知限制

- 没有后台执行器，任务不会离开 `queued`。
- 没有 Docker 或 vLLM 控制能力。
- 没有下载、日志流、资源指标和 Web UI。
- 默认只适用于本机可信环境，尚未加入认证。
- `running` 容器的重启对账要到阶段 2 实现。

## 阶段结论

阶段 1 的持久化任务与事件底座已经完成，具备接入真实 vLLM 部署执行器的条件。阶段 2 尚未开始，等待用户明确验收。
