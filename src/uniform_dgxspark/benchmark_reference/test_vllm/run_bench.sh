#!/usr/bin/env bash
# 跑一组 test_vllm 压测(两个 IO 组合),结果带实验标签写入 result/。
# 用法: bash run_bench.sh <LABEL>
#   LABEL 例如 Aprime / B / D
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
LABEL=${1:?需要实验标签, 例如: bash run_bench.sh B}
MODEL=/home/dgx-spark/models/nv-community--Gemma-4-26B-A4B-NVFP4/snapshots/master
RESULT_DIR=/home/dgx-spark/models/nv-community--Gemma-4-26B-A4B-NVFP4/result
CONDA_ENV=${CONDA_ENV:-vllm025}
PORT=${PORT:-8000}
METRICS_URL=${METRICS_URL:-http://127.0.0.1:$PORT/metrics}
cd /home/dgx-spark/models/nv-community--Gemma-4-26B-A4B-NVFP4/test_vllm

run_one() {
  local IN=$1 OUT=$2
  local PREFIX="$RESULT_DIR/bench_${LABEL}_${IN}_${OUT}"
  echo "=== [$LABEL] input=$IN output=$OUT ==="
  python3 "$SCRIPT_DIR/collect_prefill_metrics.py" snapshot \
    --url "$METRICS_URL" --output "${PREFIX}_metrics_before.json" || true
  conda run -n "$CONDA_ENV" --no-capture-output python perf_benchmark.py \
    --model gemma4_26b_nvfp4 --dataset unused --tokenizer "$MODEL" \
    --host 127.0.0.1 --port "$PORT" \
    --input "$IN" --output "$OUT" --concurrency 1 --num 5 \
    --timeout 900 --cooldown 5 \
    --csv-path "$RESULT_DIR/bench_${LABEL}_${IN}_${OUT}.csv" \
    --output_file "${PREFIX}.json" \
    --tp 1 --max-model-len 5120 --dtype nvfp4 2>&1 \
    | grep -iE "Successful requests|Benchmark duration|Total generated|Output token throughput|Mean TTFT|Mean TPOT|Mean ITL" | grep -vi Namespace
  python3 "$SCRIPT_DIR/collect_prefill_metrics.py" snapshot \
    --url "$METRICS_URL" --output "${PREFIX}_metrics_after.json" || true
  if [ -f "${PREFIX}_metrics_before.json" ] && [ -f "${PREFIX}_metrics_after.json" ]; then
    python3 "$SCRIPT_DIR/collect_prefill_metrics.py" delta \
      --before "${PREFIX}_metrics_before.json" \
      --after "${PREFIX}_metrics_after.json" \
      --output "${PREFIX}_prefill.json" || true
  fi
  echo "--- CSV ---"; cat "${PREFIX}.csv"
  if [ -f "${PREFIX}_prefill.json" ]; then
    echo "--- PREFILL ---"; cat "${PREFIX}_prefill.json"
  fi
}

run_one 2048 200
run_one 200 2048
