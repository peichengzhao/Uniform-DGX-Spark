# 阶段 0 验收报告：DGX Spark / vLLM 基线

- 日期：2026-08-28
- 状态：测试完成，等待用户验收
- 工作目录：`/home/zaki/Downloads/uniform_dgxspark`
- 本阶段未实现业务功能，也未进入阶段 1。

## 环境基线

| 项目 | 实测值 |
| --- | --- |
| 操作系统 | Ubuntu 24.04.3 LTS |
| 架构 | ARM64 / aarch64 |
| CPU | 20 核 ARM（Cortex-X925 + Cortex-A725） |
| GPU | NVIDIA GB10，计算能力 12.1 |
| 驱动 | 580.126.09 |
| 主机报告 CUDA | 13.0 |
| 系统内存 | 121 GiB，测试前可用约 109 GiB |
| Swap | 15 GiB，测试前已使用约 8 GiB |
| 工作盘可用空间 | 约 2.6 TiB |
| Docker | 29.1.3 |
| NVIDIA Container Toolkit | 1.18.2 |
| vLLM 镜像 | `nvcr.io/nvidia/vllm:26.07-py3` |
| 镜像架构 | linux/arm64 |
| 镜像摘要 | `sha256:95c498a475142c20c989c65e5d223348c09fed83ba17ddf44f117610c0bd3268` |
| vLLM | `0.24.0+092c4842.dev` |
| 容器 PyTorch | `2.13.0a0+9186a08b2c.nv26.07` |
| 容器 CUDA | 13.3 |

`nvidia-smi` 在 GB10 上显示显存为 `Not Supported`。因此平台后续不能只依赖传统显存字段，必须结合系统统一内存、进程和容器指标。

## 测试结果

| 测试 | 结果 | 证据摘要 |
| --- | --- | --- |
| Docker 可用 | 通过 | Docker daemon 可查询，测试前无运行中容器 |
| GPU 容器透传 | 通过 | 临时容器内 `nvidia-smi` 正确识别 NVIDIA GB10 |
| vLLM/PyTorch 导入 | 通过 | vLLM 和 PyTorch 均可导入 |
| CUDA 可用 | 通过 | `torch.cuda.is_available() == True` |
| GPU 能力识别 | 通过 | `torch.cuda.get_device_capability(0) == (12, 1)` |
| vLLM 服务启动 | 通过 | 本地随机最小 Qwen2 模型完成加载，API 服务启动 |
| 健康检查 | 通过 | `GET /health` 返回 HTTP 200 |
| 模型目录接口 | 通过 | `GET /v1/models` 返回 `phase0-qwen` |
| Chat Completions | 通过 | `POST /v1/chat/completions` 返回 HTTP 200，生成 8 tokens |
| Completions | 通过 | 容器重启后 `POST /v1/completions` 成功生成 2 tokens |
| Prometheus 指标 | 通过 | prompt=32、generation=8、成功请求数=1、TTFT count=1 |
| 停止与重启 | 通过 | 首次停止退出码 0，重启后约 20 秒恢复健康 |
| 最终资源清理 | 通过 | 临时服务容器已停止，退出码 0 |

用于最终 GPU 推理验证的是本地生成的随机微型 Qwen2 权重：2 层、hidden size 64、4 个 attention heads、head dimension 16。它只用于验证计算和 API 链路，不用于验证输出质量，也不会加入产品模型目录。

## 测试中发现的问题

### 1. 容器不会自动获得主机代理

主机可访问 Hugging Face，但第一次启动容器时配置请求被重置。把主机代理配置显式注入容器后，模型元数据和权重下载恢复正常。

后续要求：平台配置层统一管理代理，并分别传递给下载任务、镜像拉取和必要的推理容器；UI 不得泄露代理凭证。

### 2. 当前网络下 Hugging Face 大文件下载较慢

真实的 `Qwen/Qwen2.5-0.5B-Instruct` 已固定到 revision `7ae557604adf67be50417f59c2c2f167def9a775` 进行尝试。Xet 下载可连接，但吞吐较低；为避免阶段 0 长时间等待，主动终止了该次下载。

后续要求：阶段 3 必须测试断点续传、代理、Xet/普通 HTTP 策略、速度展示和失败恢复。不能让 vLLM 在启动过程中隐式承担平台下载任务。

### 3. 仅通过健康检查不足以判定部署成功

公开测试模型 `yujiepan/qwen2.5-tiny-random` 可以成功启动并通过 `/health`，但其 attention head dimension 为 2，低于 FlexAttention 的最低值 16，首次推理触发 HTTP 500。

后续要求：部署完成条件必须同时包含：容器存活、健康检查、模型列表检查和一次真实生成冒烟测试。

### 4. 当前目录原本为空且不是 Git 仓库

阶段 0 创建了计划和验收文档，但尚未初始化 Git，也没有应用代码。Git 初始化和工程骨架应在阶段 1 经用户验收授权后完成。

## 残留资源

- 已停止容器：`uniform-dgxspark-phase0-vllm`，用于保留测试日志。
- 临时目录：`/tmp/uniform-dgxspark-phase0-hf-cache`。
- 临时目录：`/tmp/uniform-dgxspark-phase0-tiny-cache`。
- 临时目录：`/tmp/uniform-dgxspark-phase0-local-qwen`。
- 临时文件：`/tmp/uniform-dgxspark-phase0-range.bin`。
- 未拉取新 Docker 镜像；使用的是本机原有镜像。

这些资源不会自动参与后续产品运行。用户验收阶段 0 后，可在阶段 1 开始前安全删除或保留为诊断证据。

## 用户验收方法

### 自动验收

在项目目录运行：

```bash
./scripts/accept-stage-0.sh
```

脚本会自动完成以下操作：

1. 启动阶段 0 临时 vLLM 容器。
2. 等待 `/health` 返回成功。
3. 验证 `/v1/models` 中存在 `phase0-qwen`。
4. 调用 OpenAI 兼容的 `/v1/completions` 接口。
5. 验证 vLLM Prometheus 指标存在。
6. 停止临时容器并检查退出码。

预期最后一行：

```text
PASS: stage 0 acceptance checks succeeded.
```

任何 `FAIL:`、非零脚本退出码、容器异常退出或 HTTP 请求失败都表示验收不通过。测试结束后容器应为 `exited`，退出码应为 `0`。

### 人工复核

可以运行以下命令确认最终没有遗留运行中的测试服务：

```bash
docker inspect uniform-dgxspark-phase0-vllm \
  --format 'status={{.State.Status}} exit={{.State.ExitCode}}'
```

预期输出：

```text
status=exited exit=0
```

随机微型模型只用于检查推理链路，因此不需要判断回答内容是否有语义，只检查接口成功、token 数量正确以及容器正常退出。

## 阶段结论

DGX Spark 上的 ARM64 Docker、GPU 透传、当前 NVIDIA vLLM 镜像、模型加载、OpenAI 兼容 API、指标接口和重复启动链路均已验证可用。

阶段 0 建议验收通过。阶段 1 尚未开始；等待用户明确批准。
