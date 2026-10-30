#!/usr/bin/env bash
# 受管 sglang MTP=1 启动脚本（sglang_fix 环境）。
# EAGLE 算法 + 同路径 draft model（自动 remap 为 Qwen3_5ForCausalLMMTP）。
# 用法: MODE=serve bash start_serve_mtp1.sh   (预编译请先跑 MODE=precompile)
set -euo pipefail

MODEL_PATH=/home/dgx-spark/models/nv-community--Qwen3.6-35B-A3B-NVFP4/snapshots/master
SERVED_MODEL=nvidia/Qwen3.6-35B-A3B-NVFP4
RESULT_DIR=/home/dgx-spark/models/nv-community--Qwen3.6-35B-A3B-NVFP4/result
CONDA_ENV=${CONDA_ENV:-sglang_fix}
PORT=${PORT:-8000}
TAG=${TAG:-mtp1}
MODE=${MODE:-serve}
MEM_FRACTION=${MEM_FRACTION:-0.60}
MAX_JOBS=${MAX_JOBS:-1}
CG_DECODE=${CG_DECODE:-full}
CG_BATCHES=${CG_BATCHES:-"1 2 4"}
LOG="$RESULT_DIR/serve_sglang_${TAG}.log"
HEALTH_TIMEOUT_SEC=${HEALTH_TIMEOUT_SEC:-2400}

mkdir -p "$RESULT_DIR"

preflight() {
  local mem_avail swap_free
  mem_avail=$(awk '/MemAvailable/ {printf "%.0f", $2/1024/1024}' /proc/meminfo)
  swap_free=$(awk '/SwapFree/ {printf "%.0f", $2/1024/1024}' /proc/meminfo)
  echo "[$(date +%T)] preflight: MemAvailable=${mem_avail}GiB SwapFree=${swap_free}GiB"
  if [ "$mem_avail" -lt 80 ]; then echo "ERROR: MemAvailable < 80 GiB"; return 1; fi
  if [ "$swap_free" -lt 48 ]; then echo "ERROR: SwapFree < 48 GiB"; return 1; fi
  local leftover
  leftover=$(pgrep -f "sglang.launch_server|nvcc|cicc" || true)
  if [ -n "$leftover" ]; then echo "ERROR: leftover: $leftover"; return 1; fi
  return 0
}

export PATH="/home/dgx-spark/miniconda3/envs/${CONDA_ENV}/bin:$PATH"
export PYTORCH_CUDA_ALLOC_CONF=${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}
export HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-1}
export USE_TRITON_W8A8_FP8_KERNEL=${USE_TRITON_W8A8_FP8_KERNEL:-0}
export MAX_JOBS="$MAX_JOBS"
unset FLASHINFER_DISABLE_JIT

if [ "$MODE" = serve ]; then
  preflight || { echo "ERROR: preflight failed"; exit 1; }
fi

echo "[$(date +%T)] 启动 sglang ${TAG} (env=$CONDA_ENV, mode=$MODE, mem_fraction=$MEM_FRACTION) ..."
echo "日志: $LOG"

python -m sglang.launch_server \
  --model-path "$MODEL_PATH" \
  --served-model-name "$SERVED_MODEL" \
  --host 0.0.0.0 --port "$PORT" \
  --tp-size 1 \
  --trust-remote-code \
  --quantization modelopt_mixed \
  --kv-cache-dtype fp8_e4m3 \
  --attention-backend flashinfer \
  --mem-fraction-static "$MEM_FRACTION" \
  --context-length 262144 \
  --chunked-prefill-size 32768 \
  --max-running-requests 4 \
  --load-format fastsafetensors \
  --moe-runner-backend flashinfer_cutlass \
  --fp4-gemm-backend flashinfer_cutlass \
  --fp8-gemm-backend auto \
  --linear-attn-backend triton \
  --disable-flashinfer-autotune \
  --cuda-graph-backend-decode "$CG_DECODE" \
  --cuda-graph-backend-prefill disabled \
  --cuda-graph-bs-decode $CG_BATCHES \
  --reasoning-parser qwen3 \
  --tool-call-parser qwen3_coder \
  --speculative-algorithm EAGLE \
  --speculative-draft-model-path "$MODEL_PATH" \
  --speculative-draft-load-format fastsafetensors \
  --speculative-num-steps 1 \
  --speculative-eagle-topk 1 \
  --speculative-num-draft-tokens 2 \
  ${EXTRA_ARGS:-} \
  > "$LOG" 2>&1 &
SERVE_PID=$!
echo "sglang PID=$SERVE_PID"

echo "[$(date +%T)] 等待 /health 就绪 ..."
ready=0
for i in $(seq 1 $((HEALTH_TIMEOUT_SEC / 10))); do
  if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    ready=1; echo "[$(date +%T)] sglang 就绪 (约 $((i*10))s)"; break
  fi
  if ! kill -0 "$SERVE_PID" 2>/dev/null; then
    echo "ERROR: sglang 进程已退出,日志尾部:"; tail -40 "$LOG"; exit 1
  fi
  sleep 10
done

if [ "$MODE" = precompile ]; then
  if [ "$ready" = 1 ]; then
    echo "[$(date +%T)] 预编译完成"; kill -TERM "$SERVE_PID" 2>/dev/null || true
    wait "$SERVE_PID" 2>/dev/null || true; exit 0
  fi
  echo "ERROR: 预编译未就绪"; tail -40 "$LOG"; exit 1
fi

if [ "$ready" != 1 ]; then
  echo "ERROR: ${HEALTH_TIMEOUT_SEC}s 未就绪"; tail -40 "$LOG"; exit 1
fi

if [ "$MODE" = serve ]; then
  echo "[$(date +%T)] serve 模式运行中..."; wait "$SERVE_PID"
fi
