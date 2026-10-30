# 阶段 5 验收：系统、容器与推理监控

## 已实现

- CPU、统一内存、Swap、磁盘、网络累计流量。
- NVIDIA GB10 利用率、温度和功耗。
- 明确标注 DGX Spark 使用统一内存，不伪造传统显存值。
- 受管容器 CPU、内存、网络、块 IO 和进程数量。
- vLLM 请求量、prompt tokens、generation tokens 和 TTFT 指标。
- 内存中最近 300 个采样点，默认每 2 秒采样。

## 自动验收

```bash
./scripts/accept-stage-5.sh
```

预期：

```text
PASS: stage 5 acceptance checks succeeded.
```

## 人工验收

1. 打开 Web 页面的“系统监控”。
2. 确认 GPU 名称为 NVIDIA GB10，并显示利用率、温度和功耗。
3. 确认统一内存总量约 121 GiB。
4. 启动模型并发送请求，任务详情中的生成 token 和请求数应增长。
5. 停止模型后，平台仍应保留任务事件和日志。

