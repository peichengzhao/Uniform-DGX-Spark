#!/usr/bin/env bash
# 启动 vllm serve —— nv-community Gemma-4-26B-A4B NVFP4 量化版。
#  - GB10 统一内存(CPU/GPU 共用 121GB)。NVFP4 权重 ~18GiB。
#  - DGX Spark(sm_121a)必须设 CUTE_DSL_ARCH,否则 CuteDSL/B12X NVFP4 kernel 慢 2x。
#  - Gemma4 多模态模型,压测纯文本,故 --limit-mm-per-prompt 关闭图像/视频。
#  - 需 input/output 达 2048,故 max-model-len >= 2048+2048。
#
# 单流(concurrency=1)调优要点:
#  - 默认 ENFORCE_EAGER=0 -> 开启 torch.compile + CUDA graph,消除逐算子 kernel
#    启动开销(单流 decode 的主要瓶颈)。首次启动会因编译/图捕获慢数分钟。
#  - CUDAGRAPH_SIZES: concurrency=1 只需捕获小 batch,缩短捕获时间/省显存。
#  - MOE_BACKEND: 可选强制 NVFP4 MoE 后端(auto/flashinfer_b12x/cutlass/marlin...)。
#    留空则 vLLM 自动选择。flashinfer_b12x 是 SM12x 原生融合 kernel(可能初始化失败)。
set -euo pipefail

MODEL=/home/dgx-spark/models/nv-community--Gemma-4-26B-A4B-NVFP4/snapshots/master
RESULT_DIR=/home/dgx-spark/models/nv-community--Gemma-4-26B-A4B-NVFP4/result
LOG=${SERVE_LOG:-$RESULT_DIR/serve.log}
CONDA_ENV=${CONDA_ENV:-vllm025}

export PYTORCH_CUDA_ALLOC_CONF=${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}
export VLLM_ALLOW_LONG_MAX_MODEL_LEN=${VLLM_ALLOW_LONG_MAX_MODEL_LEN:-0}
export CUTE_DSL_ARCH=${CUTE_DSL_ARCH:-sm_121a}

GPU_UTIL=${GPU_UTIL:-0.72}
MAX_MODEL_LEN=${MAX_MODEL_LEN:-5120}
MAX_NUM_SEQS=${MAX_NUM_SEQS:-8}
MAX_NUM_BATCHED=${MAX_NUM_BATCHED:-2048}
ENFORCE_EAGER=${ENFORCE_EAGER:-0}
CUDAGRAPH_SIZES=${CUDAGRAPH_SIZES:-1 2 4}
MOE_BACKEND=${MOE_BACKEND:-}
PORT=${PORT:-8000}

ARGS=(
  "$MODEL"
  --served-model-name gemma4_26b_nvfp4
  --trust-remote-code
  --host 0.0.0.0 --port "$PORT"
  --gpu-memory-utilization "$GPU_UTIL"
  --max-model-len "$MAX_MODEL_LEN"
  --max-num-seqs "$MAX_NUM_SEQS"
  --max-num-batched-tokens "$MAX_NUM_BATCHED"
  --enable-chunked-prefill
  --limit-mm-per-prompt '{"image":0,"video":0}'
)
if [ "$ENFORCE_EAGER" = "1" ]; then
  ARGS+=(--enforce-eager)
else
  # 仅在非 eager 时才配 cudagraph 捕获尺寸(eager 下会被清零,配了也无效)。
  [ -n "$CUDAGRAPH_SIZES" ] && ARGS+=(--cudagraph-capture-sizes $CUDAGRAPH_SIZES)
fi
[ -n "$MOE_BACKEND" ] && ARGS+=(--moe-backend "$MOE_BACKEND")

echo "启动 vllm serve (env=$CONDA_ENV, NVFP4): CUTE_DSL_ARCH=$CUTE_DSL_ARCH GPU_UTIL=$GPU_UTIL MAX_MODEL_LEN=$MAX_MODEL_LEN ENFORCE_EAGER=$ENFORCE_EAGER CUDAGRAPH_SIZES='${CUDAGRAPH_SIZES}' MOE_BACKEND='${MOE_BACKEND:-auto}'"
echo "日志: $LOG"
exec conda run -n "$CONDA_ENV" --no-capture-output vllm serve "${ARGS[@]}" > "$LOG" 2>&1
