#!/usr/bin/env bash
# 跑一组 sglang 压测(两个 IO 形状),结果写入 result/。
# 用法: bash run_bench.sh <LABEL> [NUM]  LABEL ∈ {nomtp, mtp1, fix, ...}  NUM 默认 20
# 复用 test_vllm/perf_benchmark.py,与 vLLM/Atlas 完全同口径。
set -euo pipefail

LABEL=${1:?用法: bash run_bench.sh <LABEL> [NUM]}
NUM=${2:-20}

MODEL_SNAP=/home/dgx-spark/models/nv-community--Qwen3.6-35B-A3B-NVFP4/snapshots/master
RESULT_DIR=/home/dgx-spark/models/nv-community--Qwen3.6-35B-A3B-NVFP4/result
CONDA_ENV=${CONDA_ENV:-vllm025}
PORT=${PORT:-8000}
BENCH_PY=/home/dgx-spark/models/nv-community--Qwen3.6-35B-A3B-NVFP4/test_vllm/perf_benchmark.py
SERVED_MODEL=nvidia/Qwen3.6-35B-A3B-NVFP4
TAG=sglang_${LABEL}
cd /home/dgx-spark/models/nv-community--Qwen3.6-35B-A3B-NVFP4/test_sglang

run_one() {
  local IN=$1 OUT=$2
  echo "=== [$TAG] input=$IN output=$OUT num=$NUM ==="
  rm -f "$RESULT_DIR/bench_${TAG}_${IN}_${OUT}.csv" "$RESULT_DIR/bench_${TAG}_${IN}_${OUT}.json"
  conda run -n "$CONDA_ENV" --no-capture-output python "$BENCH_PY" \
    --model "$SERVED_MODEL" --dataset unused --tokenizer "$MODEL_SNAP" \
    --host 127.0.0.1 --port "$PORT" \
    --input "$IN" --output "$OUT" --concurrency 1 --num "$NUM" \
    --timeout 3600 --cooldown 5 \
    --csv-path "$RESULT_DIR/bench_${TAG}_${IN}_${OUT}.csv" \
    --output_file "$RESULT_DIR/bench_${TAG}_${IN}_${OUT}.json" \
    --tp 1 --max-model-len 5120 --dtype nvfp4 2>&1 \
    | grep -iE "Successful requests|Benchmark duration|Total generated|Output token throughput|Mean TTFT|Mean TPOT|Mean ITL" | grep -vi Namespace
  echo "--- CSV ---"; cat "$RESULT_DIR/bench_${TAG}_${IN}_${OUT}.csv"
}

run_one 2048 200
run_one 200 2048
