# 精度评测数据集中心：开发与验收

日期：2026-10-27。

## 交付范围

精度评测页新增与模型中心同风格的数据集卡片，保留原有 AI 助手、手动配置、历史清理、成绩、比较与缓存入口。

目录来自本机 EvalScope 的原生基准索引和已生成元数据，共 275 项：文本模型 159、多模态 77、智能体 33、内容生成 6。不包含 OpenCompass、VLMEvalKit 等额外后端的独立目录，也不代表这些数据集已下载。

平台实际接入仍只有 GSM8K、ARC、C-Eval；其余 272 项明确显示“尚未接入”，只能浏览。目录展示没有扩展请求 schema、后端执行白名单、模型权限或评测能力。

## 阶段一：可信只读目录

新增 `scripts/export-accuracy-datasets.py` 和静态目录 `src/uniform_dgxspark/web/accuracy-datasets.json`。

导出器核对平台固定的 EvalScope commit，拒绝基准目录未提交改动；按 `_index.json` 中的规范名称导出，而非猜测 adapter 文件名。仅读取 JSON，不导入 EvalScope 插件、不下载数据、不执行评测。接入状态和允许子集来自现有 `EvalScopeAccuracyAdapter.dataset_catalog()`，不另建执行白名单。元数据附 SHA-256 来源指纹。

固定来源：`0.0.0_dev / ccb9019ff56fd7dcc8602f4d17b706b31228edca`。

可验证命令（在项目目录执行）：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/export-accuracy-datasets.py \
  --source /home/dgx-spark/Downloads/evalscope \
  --output src/uniform_dgxspark/web/accuracy-datasets.json --check
```

预期：275 项目录、3 项接入；`--check` 不写文件。升级 EvalScope 或增加真实适配后，应重新导出并验证，不能仅手工修改目录状态来开放执行。

## 阶段二：卡片与可浏览详情

提供名称、能力中文标签、数据源及子集搜索，接入状态/模型类型/能力筛选，每页 12 张卡片以及分页计数。详情展示全部子集、目录声明指标、来源版本与上游介绍。

指标声明不是成绩；上游文档中的配置不是平台实际配置。默认生成中文简介，无中文翻译时使用原文。未知数据规模、许可证和硬件需求没有猜测补齐。

上游文字始终转义或按纯文本展示，不渲染为 HTML，不把文档或评测提示当平台指令。目录加载失败时可以重试，不影响原有评测入口。

## 阶段三：已接入数据集的配置入口

卡片与详情中的“手动配置”和“AI 评测”只打开已有弹窗，不创建会话、不确认方案、不请求模型。

先选择运行中的模型。单数据集配置默认：GSM8K `main`、ARC `ARC-Challenge`、C-Eval `computer_network`；正式样本上限 10，0-shot、seed 42。AI 入口预填 12 条总预算，包含至多 2 条试跑，并明确要求检查、解释及等待平台确认。用户仍可修改方案，不能把小样本结果当完整官方成绩。

功能禁用、只读或已有精度任务运行时，配置按钮禁用，详情仍可浏览。AI 解读与实际执行权限保持原有边界。

## 阶段四：验证与上线

新增目录一致性和浏览器验证：桌面/390px 手机、搜索/筛选/分页、C-Eval 全子集、单数据集配置、AI 预算、未知数据集无执行入口、上游 HTML 注入防护、只读/禁用/运行中任务限制、加载失败重试，以及浏览不会创建评测或 AI 会话。

已完成：

- 目录可重复导出校验：275 项、3 项接入。
- 相关目录、配置 API 和 AI 工作流测试：45 passed，67.56 秒，无失败或跳过。
- 最终合并回归（目录、真实浏览器、新入口限制、原有评测页面、配置 API 与 AI 工作流）：58 passed，143.26 秒，无失败或跳过。浏览器测试强制使用真实 Chromium，评测与 Codex 用隔离替身，不调用真实模型。
- Ruff 检查、四个新增 Python 文件格式检查以及前端 JavaScript 语法检查均通过。
- 当前平台直接读取新目录返回 200；平台 `/api/v1/health` 和模型端口 8000 `/health` 均返回 200。
- 真实 Chromium 已确认 275 项目录、3 张已接入卡片，手机页面无横向溢出。

最初测试因网络沙箱限制阻塞，最小 TestClient 复现后通过本地通信权限解决；之后修正了测试中未选择目标和误把关闭详情弹窗的按钮计入当前目录的断言。本节通过结果来自修正后的实际运行，不使用历史测试成绩。

本次只更新静态资源，无需重启平台或模型，没有启动真实精度评测，没有停止/删除任何生产会话、成绩或缓存。刷新浏览器进入“精度评测 → 评测数据集”即可使用。

上线截图：

- `/home/dgx-spark/Documents/Codex/2026-10-20/name-base-url-https-aiserver-sunrise/outputs/accuracy-dataset-center-20261027.png`
- `/home/dgx-spark/Documents/Codex/2026-10-20/name-base-url-https-aiserver-sunrise/outputs/accuracy-dataset-center-mobile-20261027.png`
