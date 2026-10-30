# 阶段 36：EvalScope 兼容性探针

## 阶段边界

本阶段只验证 Uniform 已登记的本机 OpenAI-compatible 模型能否被固定版本的 EvalScope 调用，
并冻结 GSM8K 报告的最小读取契约。本阶段没有新增页面、HTTP API 或数据库表，也没有修改现有部署、
性能评测和 Spark Arena 逻辑。

固定的开发期 EvalScope 身份：

- 版本：`0.0.0_dev`
- Git commit：`ccb9019ff56fd7dcc8602f4d17b706b31228edca`
- 源码：`/home/dgx-spark/Downloads/evalscope`
- 报告 schema：`2`
- GSM8K evaluation version：`v1.1`

## 自动验收

```bash
./scripts/accept-stage-36.sh
```

自动验收完全离线，使用脱敏的 v2 报告 fixture 和临时 fake OpenAI-compatible 服务，不访问真实模型，
不下载数据集。验收内容包括：

- 接受固定的 GSM8K v2 报告并读取 Accuracy、样本数和 execution summary。
- 拒绝未知报告 schema 和不完整执行结果。
- 验证 `progress.json` 完成状态。
- 验证 `/v1/models` 与 `/v1/chat/completions` 请求兼容性。
- 验证 EvalScope 命令固定为 GSM8K、`limit=2`、单并发、固定生成参数和 seed。
- 验证命令不经过 shell、不包含真实 API Key，并拒绝非回环地址。

## 真实模型验收

EvalScope 必须安装在独立环境，且该环境中的 `evalscope` 命令来自上面固定的源码 commit。选择 Uniform
中一个已验证且正在运行的本机模型，确认它出现在 `/v1/models` 后执行：

```bash
uv run python scripts/probe-evalscope-accuracy.py run \
  --base-url http://127.0.0.1:<本机端口>/v1 \
  --model '<模型 ID>' \
  --evalscope-command '<独立环境>/bin/evalscope' \
  --evalscope-source /home/dgx-spark/Downloads/evalscope \
  --work-dir /tmp/uniform-stage-36-<唯一运行名>
```

探针先验证 EvalScope 版本/commit、`/v1/models` 和 Chat Completions，再运行固定配置：

- 数据集：GSM8K
- 样本数：2
- few-shot：0（兼容性探针不额外加载训练集）
- 并发：1
- 生成参数：`temperature=0`、`max_tokens=2048`、单请求超时 300 秒
- 数据集及 ModelScope/EvalScope/Hugging Face 缓存：当前探针工作目录下，不写用户主目录
- seed：42
- 性能采集：关闭
- 进度文件：开启

## 通过标准

- EvalScope 进程退出码为 0。
- `progress.json` 最终为 `completed`。
- v2 报告包含正确模型名、`gsm8k`、Accuracy 和两个成功样本。
- execution summary 为 requested=2、succeeded=2、errored=0、incomplete=false。
- `uniform-probe-config.json`、命令和日志不包含真实密钥。
- 自动验收脚本、Pytest 和 Ruff 全部通过。

真实运行产物位于指定工作目录；`uniform-probe-result.json` 是脱敏后的汇总。首次运行 GSM8K 可能从
ModelScope 下载数据集，因此需要网络，后续可以复用 EvalScope 自身的数据集缓存。

## 2026-10-21 验收结果

- 自动测试：13 passed；另有 1 条既有 Starlette 弃用警告。
- Ruff：通过。
- 独立验收脚本：通过。
- EvalScope：`0.0.0_dev`，commit `ccb9019ff56fd7dcc8602f4d17b706b31228edca`。
- 真实模型：Uniform 托管的 `Qwen3.8-27B-FP8`，本机 `http://127.0.0.1:8000/v1`。
- 数据集：GSM8K，0-shot，`limit=2`，两条均成功完成。
- 结果：Accuracy `1.0`；requested=2、succeeded=2、errored=0、incomplete=false。
- 进度：completed，processed=2/2，100%。
- 报告：schema v2，答案提取和规则评分完成。
- 密钥检查：配置与日志仅出现 EvalScope 的 `**********` 脱敏占位符，没有真实密钥。
- 数据库 schema：未修改。
- 网络/下载：真实验收首次从 ModelScope 下载 GSM8K；自动验收不访问外网。
- 原始运行目录：`/tmp/uniform-stage36-live-pass-bKUNJ7`。

仓库中的 fixture 来自上述真实 v2 报告，仅将模型名和长数据集描述替换为稳定的脱敏值；报告结构、
指标、样本数、完成状态和真实得分保持一致。

## 已知限制

- 当前是开发探针，不会从 Uniform 数据库自动选择模型；正式目标解析器属于阶段 38。
- 当前只冻结 EvalScope v2 报告中 Uniform 首版需要的字段，不尝试镜像 EvalScope 全部数据模型。
- 当前不提供任务持久化、取消、恢复、API 或页面；这些属于阶段 37～40。
- 本阶段不启用 EvalScope 代码执行沙箱，和 Codex/Bubblewrap 沙箱无关。

## 回滚

删除以下五个阶段 36 文件即可；没有数据库回滚：

- `scripts/probe-evalscope-accuracy.py`
- `tests/fixtures/evalscope/gsm8k-report-v2.json`
- `tests/test_evalscope_compatibility.py`
- `scripts/accept-stage-36.sh`
- `docs/acceptance/stage-36-evalscope-compatibility.md`

## 下一阶段边界

阶段 37 只增加精度评测的数据契约、SQLite 表和 Store，不启动 EvalScope、不开放 API、不显示 UI。
必须在用户确认阶段 36 后才能开始。
