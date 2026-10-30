#!/usr/bin/env bash
# 启动 sglang serve —— nv-community Qwen3.6-35B-A3B-NVFP4, MTP=1。
# 对标 test_vllm/start_serve_flashqla.sh (MTP=1)。
# sglang 用 EAGLE 算法 + 同路径 draft model, 自动 remap 为 Qwen3_5ForCausalLMMTP。
set -euo pipefail

MODEL_PATH=/home/dgx-spark/models/nv-community--Qwen3.6-35B-A3B-NVFP4/snapshots/master
SERVED_MODEL=nvidia/Qwen3.6-35B-A3B-NVFP4
RESULT_DIR=/home/dgx-spark/models/nv-community--Qwen3.6-35B-A3B-NVFP4/result
LOG="$RESULT_DIR/serve_sglang_mtp1.log"
CONDA_ENV=${CONDA_ENV:-sglang}
PORT=${PORT:-8000}

export PYTORCH_CUDA_ALLOC_CONF=${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}
export HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-1}
export USE_TRITON_W8A8_FP8_KERNEL=${USE_TRITON_W8A8_FP8_KERNEL:-1}

mkdir -p "$RESULT_DIR"

echo "[$(date +%T)] 启动 sglang MTP=1 (env=$CONDA_ENV) ..."
echo "日志: $LOG"
nohup conda run -n "$CONDA_ENV" --no-capture-output \
  python -m sglang.launch_server \
  --model-path "$MODEL_PATH" \
  --served-model-name "$SERVED_MODEL" \
  --host 0.0.0.0 --port "$PORT" \
  --tp-size 1 \
  --trust-remote-code \
  --quantization modelopt_mixed \
  --kv-cache-dtype fp8_e4m3 \
  --attention-backend flashinfer \
  --mem-fraction-static 0.65 \
  --context-length 262144 \
  --chunked-prefill-size 32768 \
  --max-running-requests 4 \
  --load-format fastsafetensors \
  --moe-runner-backend flashinfer_cutlass \
  --fp4-gemm-backend flashinfer_cutlass \
  --reasoning-parser qwen3 \
  --tool-call-parser qwen3_coder \
  --speculative-algorithm EAGLE \
  --speculative-draft-model-path "$MODEL_PATH" \
  --speculative-draft-load-format fastsafetensors \
  --speculative-num-steps 1 \
  --speculative-eagle-topk 1 \
  --speculative-num-draft-tokens 2 \
  > "$LOG" 2>&1 &
SERVE_PID=$!
echo "sglang PID=$SERVE_PID"

echo "[$(date +%T)] 等待 /health 就绪 ..."
for i in $(seq 1 180); do
  if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    echo "[$(date +%T)] sglang 就绪 (约 $((i*10))s)"; exit 0
  fi
  if ! kill -0 "$SERVE_PID" 2>/dev/null; then
    echo "ERROR: sglang 进程已退出,日志尾部:"; tail -40 "$LOG"; exit 1
  fi
  sleep 10
done
echo "ERROR: sglang 30min 内未就绪,日志尾部:"; tail -40 "$LOG"; exit 1
