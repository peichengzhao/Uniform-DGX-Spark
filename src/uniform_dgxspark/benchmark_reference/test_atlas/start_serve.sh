#!/usr/bin/env bash
# 启动 atlas (spark) 容器服务 nv-community Qwen3.6-35B-A3B-NVFP4 NVFP4。
# 用法: bash start_serve.sh <MTP>   MTP ∈ {0,1,2}
#   MTP=0  无 speculative
#   MTP=1  --speculative --num-drafts 1
#   MTP=2  --speculative --num-drafts 2
set -euo pipefail

MTP=${1:?用法: bash start_serve.sh <MTP>  (0|1|2)}
case "$MTP" in 0|1|2) ;; *) echo "MTP 必须是 0/1/2"; exit 2;; esac

MODEL_SNAP=/home/dgx-spark/models/nv-community--Qwen3.6-35B-A3B-NVFP4/snapshots/master
RESULT_DIR=/home/dgx-spark/models/nv-community--Qwen3.6-35B-A3B-NVFP4/result
LOG="$RESULT_DIR/serve_atlas_mtp${MTP}.log"
PORT=${PORT:-8888}
NAME=${NAME:-atlas}
IMAGE=${IMAGE:-avarok/atlas-gb10:latest}

mkdir -p "$RESULT_DIR"
docker rm -f "$NAME" >/dev/null 2>&1 || true

SPEC_ARGS=()
case "$MTP" in
  0) : ;;
  1) SPEC_ARGS=(--speculative --num-drafts 1) ;;
  2) SPEC_ARGS=(--speculative --num-drafts 2) ;;
esac

echo "[$(date +%T)] 启动 atlas MTP=$MTP (spec_args=[${SPEC_ARGS[*]:-none}]) ..."
docker run -d --name "$NAME" --network host --ipc host --gpus all \
  -e ATLAS_DISABLE_WATCHDOGS=1 \
  -v "$MODEL_SNAP:/models/qwen36:ro" \
  "$IMAGE" \
  serve /models/qwen36 \
    --model-name qwen36_nvfp4 \
    --port "$PORT" \
    --max-seq-len 5120 \
    --kv-cache-dtype fp8 \
    --kv-high-precision-layers auto \
    --gpu-memory-utilization 0.90 \
    --max-num-seqs 8 \
    --scheduling-policy slai \
    --enable-prefix-caching \
    --fp8-kv-calibration-tokens 256 \
    "${SPEC_ARGS[@]}" >/dev/null

echo "容器已启动,日志: docker logs $NAME  (另存: $LOG)"
echo "[$(date +%T)] 等待 /health 就绪 ..."
for i in $(seq 1 180); do
  if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    docker logs "$NAME" > "$LOG" 2>&1 || true
    echo "[$(date +%T)] atlas 就绪 (约 $((i*10))s)"; exit 0
  fi
  sleep 10
done
echo "ERROR: atlas 30min 内未就绪,容器日志尾部:"
docker logs "$NAME" 2>&1 | tail -40
docker logs "$NAME" > "$LOG" 2>&1 || true
exit 1
