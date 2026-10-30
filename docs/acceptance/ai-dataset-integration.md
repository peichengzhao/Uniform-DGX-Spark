# AI 数据集一键接入：分阶段开发与验收

日期：2026-10-27。本文承接 `accuracy-dataset-center.md`，不覆盖前一轮的历史验收记录。

## 交付范围与使用方法

精度评测页的“尚未接入”卡片及详情新增 **AI 一键接入**。先选择运行中的模型，打开入口并启动 Codex；接入对话、补充需求、方案、确认、试跑状态与报告都留在精度评测页，不必跳转 AI 中心。工作流支持收起对话、存档和删除；活动验证不能直接删除或存档。

接入流程：**Codex 检查绑定数据集 → 提交方案 → 用户在平台确认 → 平台请求模型试跑 2 条 → 校验真实报告与适配契约 → 注册启用 → 使用原有手动/AI 精度评测入口**。

首批具备已审计原生适配的自动接入候选为 `aime24`、`aime25`、`aime26`、`mmlu`、`hellaswag`、`boolq`、`sciq`；2026-10-30 增加 `amc` 后共 8 项。原有 GSM8K、ARC、C-Eval 保持不变。275 项目录不等于 275 项可执行；其他卡片仍可启动条件检查对话，但会明确说明尚缺专用适配，不会伪装接入成功。本版不是允许 AI 任意生成并执行适配器代码的开发沙箱。

试跑可能下载数据并请求所选模型，只有用户确认后才发生。当前新增数据集固定 0-shot、seed 42；MMLU 支持来源契约中的 57 个子集。2 条验证只证明链路可用，不能作为完整官方评测成绩。

## 阶段一：适配契约与独立注册

新增 `services/dataset_integration.py`。从本机固定 EvalScope 源码通过 AST 读取已审计基准的真实数据源、子集、版本；不通过目录导入任意插件。计算原生适配、共享答案解析与判分依赖的指纹。

固定来源：`0.0.0_dev / ccb9019ff56fd7dcc8602f4d17b706b31228edca`，路径 `/home/dgx-spark/Downloads/evalscope`。接入验证核对 CLI 身份、源码 commit 和未提交的受跟踪改动；源码或契约变更后，不沿用失效的授权。

数据库版本由 23 增至 24，增加会话工作流表 `ai_dataset_integrations` 和独立注册表 `accuracy_dataset_integrations`。删除对话不会删除已验证注册和精度报告。候选存在不等于授权；普通评测只接受原有数据集及已验证注册。验证任务使用独立 adapter，不临时放开其他普通评测的白名单。

验证点：来源契约一致、7 个候选不自动启用、未知数据集仍被后端拒绝、迁移保留旧表、会话删除后注册保留、重建 adapter 后注册可恢复。

## 阶段二：专用 Codex 接入助手

新增 `dataset_integration_mcp.py` 与独立任务类型 `dataset_integration`。专用 MCP 服务 `uniform_dataset` 只暴露四个工具：

- `dataset_inspect`：读取绑定数据集、模型目标及接入条件。
- `dataset_propose_plan`：提交固定 2 条的接入验证方案。
- `dataset_status`：读取方案、验证进度和结果。
- `dataset_record_analysis`：记录中文解释。

工具能力绑定会话和任务类型，不能跨用精度助手或部署权限。Codex 采用只读工作区、逐项批准上述工具、默认新工具仍需审核；关闭命令执行等无关能力。初始和恢复会话均保持专用配置，AI 不能安装软件、编辑生产代码、重启/停止模型、自行确认或虚构分数。目录文档、日志和提示均作为不可信数据处理。

OpenAI 官方文档访问本轮返回 403，因此没有声称获得官方文档核验；采用平台既有配置并做实际安装 CLI 的权限对照测试。真实 CLI 测试使用本机模拟模型响应，不请求外部真实模型。

验证点：accuracy/dataset 两种命名空间分别验证明确批准工具能调用、默认 prompt 工具不能调用，共 4 项；检查 shell 关闭、用户审核及跨 scope 拒绝。

## 阶段三：平台确认与证据门槛

新增只读目录/工作流 API、用户确认/取消 API 及 capability 专用工具 API。AI capability 不允许调用用户确认，即便附带用户 API Key。方案绑定数据集、子集、目标快照、模型名、样本数和契约指纹；重复、过期和目标/源码变化的确认均拒绝。

平台通过现有 AccuracyManager 和 EvalScope 启动验证。只有状态 completed、2/2 样本完整、无错误、质量标记有效、原生主指标为 `acc:mean` 或 `accuracy:mean`、数值处于 0..1 且判分方向有效，才可事务性注册。0 分仍可证明完整链路，不能把高分作为接入门槛。取消、坏报告、样本缺失及验证期间目标/源码变化均不得激活。重启中断的验证标记 interrupted，不偷偷恢复执行。

验证点：确认前零评测请求、重复确认不会创建第二个验证、取消竞态不注册、坏报告不注册、0 分完整报告可注册、活动任务禁止删除/存档。

## 阶段四：页面卡片与内嵌对话

静态 275 项目录叠加实时后端注册状态。未接入卡片显示“AI 一键接入”，已接入卡片显示原有“手动配置 / AI 评测”。只打开配置弹窗不会创建会话或请求模型。

新增精度页内 Codex 终端、补充需求输入、确认/取消、报告与工作流清理入口。新注册基准可在手动配置中动态预填，复跑和 AI 评测也使用后端允许的子集。保留原有精度任务、缓存、比较、AI 中心及模型中心功能。AIME 与 GSM8K 详情显示数值答案判分，其余首批选择题显示选择题判分。

验证点：真实 Chromium 桌面和 390px 手机覆盖卡片入口、页面内对话、双向补充文字、确认、验证后卡片变更、手动/AI 预填；复杂基准不出现假确认或假评测入口；上游 HTML 不执行；手机无横向溢出。

## 阶段五：回归与上线

本轮全量测试 **277 passed，347.53 秒，无失败或跳过**。强制开启浏览器与实际 Codex 可用性检查，不能以缺依赖跳过冒充验收。评测流程测试使用隔离替身，不调用生产模型；真实 CLI 的 4 项权限对照也包含在回归范围内。随后修正 AIME 详情标签并补跑相关页面/目录测试，**9 passed，30.80 秒**。

最终入口说明文字调整后，页面静态契约测试补验 **3 passed，5.72 秒**。Ruff 全项目检查（`--no-cache`）和前端 JavaScript 语法检查通过。此前目录重复导出检查仍为 275 项 / 3 项原有接入，未修改静态目录来授予执行权限。

可复验命令（项目目录）：

```bash
PATH="$PWD/.runtime/node-v22.23.3-linux-arm64/bin:$PATH" \
PYTHONDONTWRITEBYTECODE=1 UNIFORM_REQUIRE_BROWSER=1 UNIFORM_REQUIRE_CODEX=1 \
.venv/bin/python -m pytest -q -o cache_dir=/tmp/uniform-dataset-onboarding-tests tests

.venv/bin/ruff check --no-cache src tests scripts/export-accuracy-datasets.py
.runtime/node-v22.23.3-linux-arm64/bin/node --check src/uniform_dgxspark/web/app.js
```

上线前以 SQLite backup 保留一致性数据库备份：

`/home/dgx-spark/Downloads/uniform_dgxspark-plugin/data/backups/uniform-before-dataset-onboarding-20261027-_isuwldy.db`

备份与上线前数据库版本为 23，已有 3 条精度记录、6 条 AI 会话。重载前检查 AI、精度、性能、竞技场及部署任务均无活动操作；只重载 `uniform-dgxspark.service`，不停止运行中的模型容器。

上线只读验证已完成：

- 用户服务状态 active；平台 `/api/v1/health` 返回 200，数据库健康，恢复任务 0。
- 新目录与接入工作流接口均返回 200：目录 275 项，当前实际允许 GSM8K、ARC、C-Eval，7 个候选显示可进入自动接入流程；生产新增接入工作流与注册均为 0。
- 数据库实际迁移版本 24，`quick_check` 为 ok；已有 3 条精度记录、6 条 AI 会话完整保留。
- 模型端口 8000 `/health` 返回 200，没有停止模型容器。
- 真实 Chromium 在当前生产页面打开 MMLU 接入弹窗，确认入口可用、目标已绑定、2 条验证提醒正确、精度页内对话区域存在；桌面和 390px 手机均无横向溢出。只打开并关闭弹窗，未提交会话或确认验证。

生产截图：

- `/home/dgx-spark/Documents/Codex/2026-10-20/name-base-url-https-aiserver-sunrise/outputs/ai-dataset-integration-desktop-20261027.png`
- `/home/dgx-spark/Documents/Codex/2026-10-20/name-base-url-https-aiserver-sunrise/outputs/ai-dataset-integration-mobile-20261027.png`

完整对话、方案确认及激活的端到端流程已在隔离浏览器测试中验证；本轮没有替用户启动生产接入会话、调用真实模型评测或确认真实验证。首次真实接入仍可能因数据下载、环境依赖或模型响应失败，需要以当次平台验证报告为准。
