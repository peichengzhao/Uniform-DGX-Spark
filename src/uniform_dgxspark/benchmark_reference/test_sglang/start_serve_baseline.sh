#!/usr/bin/env bash
# Step-1 baseline: EAGLE MTP with plan parameters (ctx 5120, mem-fraction 0.85).
# Uses the editable sglang install (mtp-memory-fix worktree) via sglang_fix env.
set -euo pipefail

MODEL_PATH=/home/dgx-spark/models/nv-community--Qwen3.6-35B-A3B-NVFP4/snapshots/master
SERVED_MODEL=nvidia/Qwen3.6-35B-A3B-NVFP4
RESULT_DIR=/home/dgx-spark/models/nv-community--Qwen3.6-35B-A3B-NVFP4/result
LOG="$RESULT_DIR/serve_sglang_${LOG_SUFFIX:-step1_baseline}.log"
CONDA_ENV=${CONDA_ENV:-sglang_fix}
PORT=${PORT:-8000}
MEM_FRACTION=${MEM_FRACTION:-0.85}
CTX=${CTX:-5120}
DRAFT_LOAD_FORMAT=${DRAFT_LOAD_FORMAT:-fastsafetensors}

export PATH="/home/dgx-spark/miniconda3/envs/${CONDA_ENV}/bin:$PATH"
export PYTORCH_CUDA_ALLOC_CONF=${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}
export HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-1}
export USE_TRITON_W8A8_FP8_KERNEL=${USE_TRITON_W8A8_FP8_KERNEL:-0}

mkdir -p "$RESULT_DIR"
echo "[$(date +%T)] 启动 sglang step1 baseline (env=$CONDA_ENV, mem=$MEM_FRACTION, ctx=$CTX) ..."
echo "日志: $LOG"

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
  --speculative-algorithm EAGLE \
  --speculative-draft-model-path "$MODEL_PATH" \
  --speculative-draft-load-format "$DRAFT_LOAD_FORMAT" \
  --speculative-num-steps 1 \
  --speculative-eagle-topk 1 \
  --speculative-num-draft-tokens 2 \
  --speculative-draft-model-quantization modelopt_mixed \
  > "$LOG" 2>&1 &
SERVE_PID=$!
echo "sglang PID=$SERVE_PID"

echo "[$(date +%T)] 等待 /health 就绪 ..."
for i in $(seq 1 240); do
  if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    echo "[$(date +%T)] sglang 就绪 (约 $((i*10))s)"; exit 0
  fi
  if ! kill -0 "$SERVE_PID" 2>/dev/null; then
    echo "ERROR: sglang 进程已退出,日志尾部:"; tail -60 "$LOG"; exit 1
  fi
  sleep 10
done
echo "ERROR: sglang 40min 内未就绪,日志尾部:"; tail -60 "$LOG"; exit 1
