#!/usr/bin/env bash
# 受管 sglang 启动脚本（sglang_fix 环境）。
#
# 模式：
#   MODE=precompile  独立预编译阶段：MAX_JOBS=1 + 不禁用 JIT，补齐版本化缓存后即退出
#   MODE=serve       正式服务：FLASHINFER_DISABLE_JIT=1，缓存缺失立即失败，启用资源守护
#
# 用法：MODE=precompile bash start_serve_fix.sh
#       MODE=serve     bash start_serve_fix.sh
# 环境变量：CONDA_ENV, PORT, TAG, MEM_FRACTION, MAX_JOBS, EXTRA_ARGS,
#           SGLANG_NVFP4_SMALL_MOE（仅 TRT-LLM runner 的小 M 实验路径）
set -euo pipefail

MODEL_PATH=/home/dgx-spark/models/nv-community--Qwen3.6-35B-A3B-NVFP4/snapshots/master
SERVED_MODEL=nvidia/Qwen3.6-35B-A3B-NVFP4
RESULT_DIR=/home/dgx-spark/models/nv-community--Qwen3.6-35B-A3B-NVFP4/result
CONDA_ENV=${CONDA_ENV:-sglang_fix}
PORT=${PORT:-8000}
TAG=${TAG:-baseline}
MODE=${MODE:-serve}
MEM_FRACTION=${MEM_FRACTION:-0.65}
MAX_JOBS=${MAX_JOBS:-1}
CG_DECODE=${CG_DECODE:-full}      # full | disabled
CG_BATCHES=${CG_BATCHES:-"1 2 4"}
LOG="$RESULT_DIR/serve_sglang_${TAG}.log"
HEALTH_TIMEOUT_SEC=${HEALTH_TIMEOUT_SEC:-2400}

mkdir -p "$RESULT_DIR"

# ---------- 启动前检查（serve 模式） ----------
preflight() {
  local mem_avail swap_free
  mem_avail=$(awk '/MemAvailable/ {printf "%.0f", $2/1024/1024}' /proc/meminfo)
  swap_free=$(awk '/SwapFree/ {printf "%.0f", $2/1024/1024}' /proc/meminfo)
  echo "[$(date +%T)] preflight: MemAvailable=${mem_avail}GiB SwapFree=${swap_free}GiB"
  if [ "$mem_avail" -lt 80 ]; then
    echo "ERROR: MemAvailable < 80 GiB"; return 1
  fi
  if [ "$swap_free" -lt 48 ]; then
    echo "ERROR: SwapFree < 48 GiB"; return 1
  fi
  local leftover
  leftover=$(pgrep -f "sglang.launch_server|nvcc|cicc" || true)
  if [ -n "$leftover" ]; then
    echo "ERROR: leftover processes: $leftover"; return 1
  fi
  return 0
}

# ---------- 资源守护（serve 模式，后台） ----------
watchdog() {
  local bad=0
  while true; do
    local mem_avail swap_free
    mem_avail=$(awk '/MemAvailable/ {printf "%.0f", $2/1024/1024}' /proc/meminfo)
    swap_free=$(awk '/SwapFree/ {printf "%.0f", $2/1024/1024}' /proc/meminfo)
    if [ "$mem_avail" -lt 8 ] || [ "$swap_free" -lt 16 ]; then
      bad=$((bad + 1))
    else
      bad=0
    fi
    if [ "$bad" -ge 2 ]; then  # ~10s 持续
      echo "[$(date +%T)] [WATCHDOG] resource pressure: MemAvailable=${mem_avail}GiB SwapFree=${swap_free}GiB -> killing process group" >> "$LOG"
      {
        echo "=== free ==="; free -g
        echo "=== top procs ==="; ps aux | sort -rk3 | head -15
        echo "=== nvidia-smi ==="; nvidia-smi
        echo "=== dmesg ==="; dmesg | tail -50
      } >> "$LOG" 2>&1
      kill -TERM -- -"$$" 2>/dev/null || true
      exit 0
    fi
    sleep 5
  done
}

# ---------- 预编译就绪校验（serve 模式） ----------
REQUIRED_KERNELS="
fp4_quantization_120f
fp4_gemm_cutlass_sm120
fused_moe_120
gdn_prefill_sm90
sampling
batch_prefill_with_kv_cache_dtype_q_bf16_dtype_kv_e4m3_dtype_o_bf16_dtype_idx_i32_head_dim_qk_256_head_dim_vo_256_posenc_0_use_swa_False_use_logits_cap_False_f16qk_False
"
assert_kernels_ready() {
  local missing=0
  for k in $REQUIRED_KERNELS; do
    if [ ! -f "$HOME/.cache/flashinfer/0.6.12/121a/cached_ops/$k/$k.so" ]; then
      echo "[$(date +%T)] MISSING JIT KERNEL: $k"
      missing=1
    fi
  done
  if [ "$missing" = 1 ]; then
    echo "ERROR: JIT kernels not precompiled. Run: MODE=precompile bash start_serve_fix.sh"
    return 1
  fi
  echo "[$(date +%T)] all required JIT kernels present"
  return 0
}

# ---------- 环境 ----------
export PATH="/home/dgx-spark/miniconda3/envs/${CONDA_ENV}/bin:$PATH"
export PYTORCH_CUDA_ALLOC_CONF=${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}
export HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-1}
export USE_TRITON_W8A8_FP8_KERNEL=${USE_TRITON_W8A8_FP8_KERNEL:-0}
export MAX_JOBS="$MAX_JOBS"
# 注意：0.6.12 的 FLASHINFER_DISABLE_JIT 只支持 AOT 部署（需 flashinfer-jit-cache 包，PyPI 无）。
# 正式模式依赖 ninja 增量判断（.so 最新则 no-op），因此统一允许 JIT 但限 MAX_JOBS=1。
# 预编译阶段负责补齐所有 .so；校验见 assert_kernels_ready()。
unset FLASHINFER_DISABLE_JIT

# 版本化缓存哈希（CUDA/Torch/FlashInfer/SM 均已体现在缓存路径与 ninja 的源码 mtime 中）
CACHE_HASH=$(python3 -c "
import hashlib, glob, os
files = glob.glob(os.path.expanduser('~/.cache/flashinfer/0.6.12/121a/cached_ops/*/*.so'))
h = hashlib.sha256()
for f in sorted(files):
    h.update(os.path.basename(f).encode())
    h.update(str(os.path.getmtime(f)).encode())
print(h.hexdigest()[:16])
")
echo "[$(date +%T)] MODE=$MODE CACHE_HASH=$CACHE_HASH mem_fraction=$MEM_FRACTION MAX_JOBS=$MAX_JOBS"

if [ "$MODE" = serve ]; then
  preflight || { echo "ERROR: preflight failed"; exit 1; }
  assert_kernels_ready || { echo "ERROR: kernels not ready"; exit 1; }
  watchdog &
  WD_PID=$!
  echo "watchdog pid=$WD_PID"
fi

echo "[$(date +%T)] 启动 sglang ${TAG} (env=$CONDA_ENV, mode=$MODE) ..."
echo "日志: $LOG"

# 受管前台进程组：本脚本与 server 同进程组，watchdog 可整体终止
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
  --disable-flashinfer-autotune \
  --cuda-graph-backend-decode "$CG_DECODE" \
  --cuda-graph-backend-prefill disabled \
  --cuda-graph-bs-decode $CG_BATCHES \
  --reasoning-parser qwen3 \
  --tool-call-parser qwen3_coder \
  ${EXTRA_ARGS:-} \
  > "$LOG" 2>&1 &
SERVE_PID=$!
echo "sglang PID=$SERVE_PID"

echo "[$(date +%T)] 等待 /health 就绪 ..."
ready=0
for i in $(seq 1 $((HEALTH_TIMEOUT_SEC / 10))); do
  if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    ready=1
    echo "[$(date +%T)] sglang 就绪 (约 $((i*10))s)"
    break
  fi
  if ! kill -0 "$SERVE_PID" 2>/dev/null; then
    echo "ERROR: sglang 进程已退出,日志尾部:"; tail -40 "$LOG"; exit 1
  fi
  sleep 10
done

if [ "$MODE" = precompile ]; then
  if [ "$ready" = 1 ]; then
    echo "[$(date +%T)] 预编译完成,关闭服务。"
    kill -TERM "$SERVE_PID" 2>/dev/null || true
    wait "$SERVE_PID" 2>/dev/null || true
    exit 0
  fi
  echo "ERROR: 预编译阶段未就绪"; tail -40 "$LOG"; exit 1
fi

if [ "$ready" != 1 ]; then
  echo "ERROR: sglang ${HEALTH_TIMEOUT_SEC}s 内未就绪,日志尾部:"; tail -40 "$LOG"; exit 1
fi

# serve 模式：保持前台，等待 server 退出
if [ "$MODE" = serve ]; then
  echo "[$(date +%T)] serve 模式运行中,等待退出..."
  wait "$SERVE_PID"
fi
