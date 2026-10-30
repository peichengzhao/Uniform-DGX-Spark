#!/usr/bin/env bash
# Non-speculative baseline server (for memory/correctness comparison).
set -euo pipefail

MODEL_PATH=/home/dgx-spark/models/nv-community--Qwen3.6-35B-A3B-NVFP4/snapshots/master
SERVED_MODEL=nvidia/Qwen3.6-35B-A3B-NVFP4
RESULT_DIR=/home/dgx-spark/models/nv-community--Qwen3.6-35B-A3B-NVFP4/result
LOG="$RESULT_DIR/serve_sglang_nospec.log"
CONDA_ENV=${CONDA_ENV:-sglang_fix}
PORT=${PORT:-8000}
MEM_FRACTION=${MEM_FRACTION:-0.85}
CTX=${CTX:-5120}

export PATH="/home/dgx-spark/miniconda3/envs/${CONDA_ENV}/bin:$PATH"
export PYTORCH_CUDA_ALLOC_CONF=${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}
export HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-1}
export USE_TRITON_W8A8_FP8_KERNEL=${USE_TRITON_W8A8_FP8_KERNEL:-0}

mkdir -p "$RESULT_DIR"
echo "[$(date +%T)] 启动 sglang no-spec baseline (env=$CONDA_ENV, mem=$MEM_FRACTION, ctx=$CTX) ..."

nohup python -m sglang.launch_server \
  --model-path "$MODEL_PATH" \
  --served-model-name "$SERVED_MODEL" \
  --host 0.0.0.0 --port "$PORT" \
  --tp-size 1 \
  --trust-remote-code \
  --quantization modelopt_mixed \
  --kv-cache-dtype fp8_e4m3 \
  --attention-backend flashinfer \
  --mem-fraction-static "$MEM_FRACTION" \
  --context-length "$CTX" \
  --chunked-prefill-size 2048 \
  --max-running-requests 4 \
  --load-format fastsafetensors \
  --moe-runner-backend flashinfer_cutlass \
  --fp4-gemm-backend flashinfer_cutlass \
  --enable-deterministic-inference \
  > "$LOG" 2>&1 &
SERVE_PID=$!
echo "sglang PID=$SERVE_PID"

for i in $(seq 1 240); do
  if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    echo "[$(date +%T)] sglang 就绪 (约 $((i*10))s)"; exit 0
  fi
  if ! kill -0 "$SERVE_PID" 2>/dev/null; then
    echo "ERROR: sglang 进程已退出,日志尾部:"; tail -60 "$LOG"; exit 1
  fi
  sleep 10
done
echo "ERROR: 超时未就绪"; tail -60 "$LOG"; exit 1
