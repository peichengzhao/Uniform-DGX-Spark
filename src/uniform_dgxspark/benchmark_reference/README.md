# AI 自定义评测参考代码

此目录随 Uniform 一起版本化，供“自定义性能测试”的 AI 会话参考。每次会话都会把它复制
到独立工作区，AI 只能修改工作副本，不应直接改动这里的基线代码。

参考实现覆盖 Atlas、SGLang 和 vLLM，包含服务启动、正确性检查与性能测试脚本。脚本中的
模型路径、端口、环境名和引擎参数来自历史实测环境，不代表当前部署；执行前必须结合会话
生成的 `benchmark-context.json` 核对并调整。

历史 `*.log`、测试结果和缓存未纳入参考库。

vLLM 参考目录中的 `collect_prefill_metrics.py` 用于在每个测试用例前后采集原生
Prometheus Prefill 计时直方图，并通过计数器差值生成独立的 Prefill 总耗时、均值、
P50/P95/P99 与吞吐。若目标引擎不暴露原生 Prefill 计时，结果必须保留为空，不能用
TTFT 或整场测试耗时替代。

`collect_unified_memory.py` 是与推理引擎无关的 DGX Spark 资源采样器。它根据
`benchmark-context.json` 自动发现本机及在线 SSH 节点，以 `/proc/meminfo` 中的
`MemTotal - MemAvailable` 作为 CPU/GPU 共享内存池的实际占用，同时通过 `nvidia-smi`
采集每个节点/设备的 GPU 利用率。它每秒采样并原子写入 JSON。正式测试时应在每个 case
前启动、case 后发送 `SIGINT` 或 `SIGTERM` 并等待退出：

```bash
python3 benchmark-reference/collect_unified_memory.py \
  --output results/case-001-unified-memory.json --interval 1 &
MEMORY_SAMPLER_PID=$!
# 在这里运行该 case 的负载测试
kill -INT "$MEMORY_SAMPLER_PID"
wait "$MEMORY_SAMPLER_PID"
```

结构化结果的 `unified_memory_peak_bytes` 使用输出中 `cluster.peak_used_bytes`；双机场景
表示同一采样轮次各节点已用统一内存之和。Head/Worker 各自峰值须从 `nodes` 写入报告。
结构化结果的 `gpu_utilization_peak_by_device_percent` 按 Head/本机设备、Worker/后续设备
顺序填写；单设备只写 GPU 0，TP 双设备依次写 GPU 0 和 GPU 1。
