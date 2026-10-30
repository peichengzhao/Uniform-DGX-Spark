# Atlas Bench — ignore_eos 修复方案

## 问题

`vllm bench serve` 默认发 `"ignore_eos": true` 让模型生成满 `max_tokens`。
**Atlas (spark) 引擎不解析 `ignore_eos` 字段**（二进制中无此字符串，未知字段被 serde 丢弃），
模型遇到 EOS 自然停止。随机 token 数据集（`--dataset-name random`）是乱码输入，
模型很早就想结束 → 部分请求只生成 1~2 个 token 就停。

### 后果

TPOT = `(请求总时长 - TTFT) / 输出token数`，分母趋近 0 时该值爆炸：
- 200/2048 档 Mean TPOT 飙到 259.75ms、P99 6306.99ms（伪值）
- 实际 decode 延迟看 ITL / Median TPOT 才准（~10ms）
- 输出吞吐不可比（Atlas 生成 token 数远少于 vLLM）

## 调试过程（系统化）

### 尝试 1: min_tokens（部分成功）

Atlas 二进制请求结构体含 `min_tokens` 字段（与 `chat_template_kwargs`、`response_format` 并列）。
`min_tokens=N` 语义 = 生成满 N token 前屏蔽 EOS，与 `ignore_eos` 等效。
`vllm bench serve` 原生支持 `--extra-body '{"min_tokens": N}'` 注入。

**验证结果**：
- 短输入(200 token) + min_tokens=200 → 200 token, finish=length ✓
- **长随机输入(2048 token) + min_tokens=200 → 2/91/167 token, finish=stop ✗**

**结论**：Atlas 的 `min_tokens` 不是硬 EOS logit mask。模型强预测 EOS 时（长乱码输入），
min_tokens 被穿透。对长输入无效，不能作为完整修复。

### 尝试 2: sonnet 真实文本数据集（成功）

根因不是参数，是**输入内容**：随机 token ID 是乱码，模型想早停；真实文本不会。

**验证**：2048-token 真实文本输入 + max_tokens=200（无 min_tokens）→ 200 token, finish=length ✓

**方案**：改用 `--dataset-name sonnet`（莎士比亚真实文本），模型自然生成满 `max_tokens`，
无需依赖 `ignore_eos` 或 `min_tokens`。

## 修复实现

### 1. `test_vllm/perf_benchmark.py`（通用压测脚本）

新增三个 CLI 选项：
- `--min-tokens`：开启时给 `vllm bench serve` 命令追加 `--extra-body '{"min_tokens": <output_len>}'`
  （短输入兜底，长输入无害）
- `--dataset-name {random,sonnet}`：数据集选择（默认 random，vLLM 侧保持不变）
- `--sonnet-path <PATH>`：sonnet 文本文件路径

命令构建器按 dataset_name 切换参数：
- `random` → `--dataset-name random --random-input-len {i} --random-output-len {o}`
- `sonnet` → `--dataset-name sonnet --dataset-path {path} --sonnet-input-len {i} --sonnet-output-len {o}`

`_parse_command_params` 正则兼容两种：`--(?:random|sonnet)-(input|output)-len`

### 2. `test_atlas/run_bench.sh` / `run_bench_n50.sh`（Atlas 专用脚本）

perf_benchmark.py 调用加：
```
--min-tokens \
--dataset-name sonnet --sonnet-path /home/dgx-spark/vllm/vllm/benchmarks/sonnet.txt \
```

vLLM 侧脚本（`test_vllm/run_bench_*.sh`）**不动**——vLLM 认 `ignore_eos`，
random 数据集即可生成足额输出，保持方法论不变。

## sonnet 文本文件

```
/home/dgx-spark/vllm/vllm/benchmarks/sonnet.txt   (517 行，莎士比亚十四行诗)
```

来自 vllm 源码仓库，无需下载。若该文件不存在，任意真实英文文本（每行一句）均可，
行数足够构造 2048-token prompt 即可。

## 验证（MTP=1, num=50, 2026-07-30）

| IO 形状 | 修复前生成 token | 修复后生成 token | 修复前 Mean TPOT | 修复后 Mean TPOT | 修复后 P99 TPOT |
|---|---|---|---|---|---|
| 2048/200 | 6405 (≈128/req) | **10000** (=200×50) | 13.78 | **9.51** | **10.64** |
| 200/2048 | 57157 (≈1143/req) | **102400** (=2048×50) | 259.75* | **9.50** | **9.65** |

修复后 TPOT ≈ ITL（~9.5ms），P99 从 6306ms → 9.65ms，统计伪值消除。

## 可比性说明

- Atlas 用 sonnet（真实文本），vLLM 用 random（随机 token）。两者均生成足额 output_len token，
  decode 工作量等价，吞吐/ITL/TPOT 可直接比。
- prefill 的 token 分布不同（真实文本 vs 随机），TTFT 对比有轻微口径差异（MoE 路由略不同），
  但同 token 数下 prefill 计算量等价，差异可忽略。
- 若需完全同口径 A/B，vLLM 侧也加 `--dataset-name sonnet --sonnet-path <path>` 重跑即可。

## 快速验证 min_tokens 是否仍需保留

```bash
# 启动 atlas 后，curl 测试 min_tokens 对长随机输入是否生效
python3 -c "import random,json;random.seed(0);ids=[random.randint(100,151000) for _ in range(2048)];print(json.dumps({'model':'qwen36_nvfp4','prompt':ids,'max_tokens':200,'min_tokens':200,'temperature':0,'stream':False,'ignore_eos':True}))" > /tmp/t.json
curl -s http://127.0.0.1:8888/v1/completions -H 'Content-Type: application/json' -d @/tmp/t.json | python3 -c "import sys,json;d=json.load(sys.stdin);print('tokens='+str(d['usage']['completion_tokens'])+' finish='+d['choices'][0]['finish_reason'])"
# 若输出 tokens=200 finish=length → min_tokens 已修复，可去掉 sonnet 改回 random
# 若 tokens<200 finish=stop → min_tokens 仍失效，继续用 sonnet
```

Atlas 版本升级后可重跑此验证；若 min_tokens 对长输入生效，可切回 random 数据集
（与 vLLM 完全同口径）。
