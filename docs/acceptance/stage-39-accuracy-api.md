# 阶段 39：精度评测应用接线与 HTTP API

## 阶段边界

本阶段把阶段 37～38 已实现的精度评测 Store、EvalScope 适配器和后台管理器接入应用生命周期，
并提供受现有全局 API Key 保护的 HTTP API。本阶段不修改前端，也不进入阶段 40。

本阶段包含：

- 应用启动时创建 Accuracy Store/Manager，并将未完成任务恢复为 `interrupted`。
- 应用关闭时取消仍在执行的 EvalScope 进程。
- 创建、分页列表、单项读取、取消和删除精度评测任务。
- 读取经过凭据脱敏的日志；支持行数上限和截断标记。
- 只列出和下载白名单内的 JSON 配置、进度与报告工件。
- 拒绝原始日志下载、路径穿越、符号链接逃逸和非白名单文件。
- 目标不存在、目标状态冲突、非法评测配置、存储不可用和功能关闭的明确 HTTP 映射。
- `UNIFORM_ACCURACY_ENABLED`、`UNIFORM_ACCURACY_EVALSCOPE_COMMAND` 和
  `UNIFORM_ACCURACY_EVALSCOPE_SOURCE` 配置入口。

## HTTP API

| 方法 | 路径 | 成功结果 |
| --- | --- | --- |
| `POST` | `/api/v1/accuracy/runs` | `201`，创建后台评测任务 |
| `GET` | `/api/v1/accuracy/runs` | `200`，分页历史列表 |
| `GET` | `/api/v1/accuracy/runs/{run_id}` | `200`，任务、进度与归一化指标 |
| `POST` | `/api/v1/accuracy/runs/{run_id}/cancel` | `200`，取消运行中任务 |
| `DELETE` | `/api/v1/accuracy/runs/{run_id}` | `204`，删除终态记录和运行目录 |
| `GET` | `/api/v1/accuracy/runs/{run_id}/logs` | `200`，脱敏后的日志尾部 |
| `GET` | `/api/v1/accuracy/runs/{run_id}/artifacts` | `200`，JSON 工件白名单 |
| `GET` | `/api/v1/accuracy/runs/{run_id}/artifacts/{path}` | `200`，下载白名单 JSON |

错误语义：不存在为 `404`，运行状态或并发冲突为 `409`，非法数据集配置为 `422`，运行目录
不可用为 `503`，功能关闭为 `403`；配置全局 API Key 后，无有效 `X-API-Key` 的请求为 `401`。

## 安全边界

- 接口沿用全局 `X-API-Key` 中间件，不增加旁路认证。
- 日志读取会脱敏 Authorization/Bearer、API Key、token、password、secret 和代理 URL 凭据。
- 日志只从当前任务目录的固定文件名读取，并拒绝文件或父目录符号链接逃逸。
- 工件下载只接受平台生成的 JSON 白名单，解析后路径必须仍位于当前运行目录。
- 原始 EvalScope 日志不可作为工件下载；下载响应固定为 JSON、禁止 MIME 嗅探且不缓存。
- 运行中的任务必须先取消，进入终态并退出管理器后才能删除。

## 自动验收

```bash
./scripts/accept-stage-39.sh
```

验收覆盖：

- 阶段 38 的 38 项后台执行、归一化与相关性能评测回归。
- 应用生命周期正确创建 Store/Manager，关闭时调用停止逻辑。
- API 创建、轮询、分页列表、详情和归一化指标闭环。
- 日志凭据脱敏及日志父目录符号链接逃逸防护。
- JSON 工件列表、下载、响应头、原始日志拒绝和路径穿越拒绝。
- 不存在目标 `404`、非运行目标 `409`、非法子集 `422`。
- 功能关闭 `403` 和 API Key 缺失 `401`。
- 运行中删除 `409`，取消后删除同时清理数据库记录和运行目录。
- 服务重启把活跃任务恢复为 `interrupted` 并计入健康接口恢复数量。
- 相关 Ruff 静态检查和 `src`/`tests` 字节码编译。

## 本次执行结果

- 执行日期：2026-10-22。
- 阶段 39 一键验收中的阶段 38 回归：`38 passed in 34.25s`。
- 阶段 39 API、鉴权和恢复相关测试：`10 passed in 19.43s`。
- 全量回归（最终复验）：`179 passed in 132.54s`。
- 全项目 Ruff：通过。
- `src` 与 `tests` compileall：通过。
- 测试未下载数据集、未访问真实模型，也未修改前端文件。

## 人工验收

本阶段没有前端页面。可在已运行模型的 `target_id` 上通过 OpenAPI 或 HTTP 客户端创建任务，随后
轮询详情接口，预期状态按 `queued → preparing → running → parsing → completed/partial` 推进。

完成后检查日志接口和工件列表：日志中不应出现真实凭据；列表只应包含
`uniform-accuracy-config.json`、`uniform-command.json`、`progress.json` 和 `reports/**/*.json`。
尝试下载 `evalscope.log` 应返回 `404`。删除终态任务应返回 `204`，再次读取应返回 `404`。

## 通过标准

- 一键验收、全量 Pytest、全项目 Ruff 和 compileall 全部通过。
- 服务重启、关闭、取消和删除不会留下活跃 Accuracy 管理状态。
- 任何日志和下载接口都不能越过任务目录或暴露未脱敏凭据。
- 原有阶段 38、性能评测、Spark Arena、鉴权与部署恢复测试无新增失败。
- 前端文件没有变化，阶段 40 功能没有提前实现。

## 已知边界与残留资源

- 尚无精度评测前端入口、表单、进度页或指标图表；这些属于阶段 40 或之后的范围。
- 自动测试使用固定假 EvalScope 进程，不下载数据集、不访问真实模型服务；真实兼容性沿用阶段 36。
- 测试资源位于 Pytest 临时目录并由测试环境清理，不写入生产数据库。
- 生产环境仍需提供阶段 36/38 固定身份的 EvalScope 可执行文件和源码目录。

## 回滚

可以移除 Accuracy 路由、应用生命周期接线、阶段 39 新增配置和接口测试。阶段 37 的数据库表、
既有评测记录以及阶段 38 的执行服务必须保留，不得通过删除数据库回滚。

## 下一阶段边界

阶段 40 可增加精度评测前端入口和结果可视化，但必须在用户确认阶段 39 后才能开始。
