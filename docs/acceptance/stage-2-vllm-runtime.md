# 阶段 2 验收：vLLM 容器部署闭环

## 已实现

- 持久化后台 worker 顺序处理部署任务。
- Docker 镜像检查/拉取、受管容器创建和 GPU 透传。
- 本地模型目录只读挂载，端口冲突检查。
- 模型架构、权重文件和统一内存预算预检。
- 健康检查后再执行一次 OpenAI-compatible 真实生成测试。
- 停止、取消、失败重试、日志读取和服务重启容器对账。
- 所有容器都有 deployment ID 标签，平台拒绝操作无相同标签的容器。

## 自动验收

```bash
./scripts/accept-stage-2.sh
```

该脚本会使用阶段 0 的微型 Qwen2 测试模型，真实启动 NVIDIA vLLM 容器、等待 `running`、生成 2 tokens、检查日志/事件/指标，再通过 API 停止并清理容器。

预期最后一行：

```text
PASS: stage 2 acceptance checks succeeded.
```

## 人工验收

1. 运行 `./scripts/run-local.sh` 并打开 `http://127.0.0.1:18180`。
2. 点击“部署模型”。
3. 模型 ID 填 `phase2-qwen`，本地目录填 `/tmp/uniform-dgxspark-phase0-local-qwen`。
4. 端口填 `18002`，上下文填 `256`，GPU 内存比例填 `0.02`。
5. 高级参数增加一行 `--enforce-eager`。
6. 观察任务依次经过环境检查、模型解析、镜像、容器、加载和健康检查。
7. 状态应最终为“运行中”；点击任务可看到日志和事件。
8. 点击“停止”，最终状态应为“已停止”。

任何 HTTP 500、任务 `failed`、无法读取日志、推理测试失败或容器无法停止均表示不通过。
