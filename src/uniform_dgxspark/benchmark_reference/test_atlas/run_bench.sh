#!/usr/bin/env bash
# 跑一组 atlas 压测(两个 IO 形状),结果写入 result/。
# 用法: bash run_bench.sh <MTP>   MTP ∈ {0,1,2}
set -euo pipefail

MTP=${1:?用法: bash run_bench.sh <MTP>  (0|1|2)}
case "$MTP" in 0|1|2) ;; *) echo "MTP 必须是 0/1/2"; exit 2;; esac

MODEL_SNAP=/home/dgx-spark/models/nv-community--Qwen3.6-35B-A3B-NVFP4/snapshots/master
RESULT_DIR=/home/dgx-spark/models/nv-community--Qwen3.6-35B-A3B-NVFP4/result
CONDA_ENV=${CONDA_ENV:-vllm025}
PORT=${PORT:-8888}
BENCH_PY=/home/dgx-spark/models/nv-community--Qwen3.6-35B-A3B-NVFP4/test_vllm/perf_benchmark.py
SONNET_PATH=/home/dgx-spark/vllm/vllm/benchmarks/sonnet.txt
LABEL=atlas_mtp${MTP}
cd /home/dgx-spark/models/nv-community--Qwen3.6-35B-A3B-NVFP4/test_atlas

run_one() {
  local IN=$1 OUT=$2
  echo "=== [$LABEL] input=$IN output=$OUT ==="
  rm -f "$RESULT_DIR/bench_${LABEL}_${IN}_${OUT}.csv" "$RESULT_DIR/bench_${LABEL}_${IN}_${OUT}.json"
  conda run -n "$CONDA_ENV" --no-capture-output python "$BENCH_PY" \
    --model qwen36_nvfp4 --dataset unused --tokenizer "$MODEL_SNAP" \
    --host 127.0.0.1 --port "$PORT" \
    --input "$IN" --output "$OUT" --concurrency 1 --num 20 \
    --timeout 1800 --cooldown 5 --min-tokens \
    --dataset-name sonnet --sonnet-path "$SONNET_PATH" \
    --csv-path "$RESULT_DIR/bench_${LABEL}_${IN}_${OUT}.csv" \
    --output_file "$RESULT_DIR/bench_${LABEL}_${IN}_${OUT}.json" \
    --tp 1 --max-model-len 5120 --dtype nvfp4 2>&1 \
    | grep -iE "Successful requests|Benchmark duration|Total generated|Output token throughput|Mean TTFT|Mean TPOT|Mean ITL" | grep -vi Namespace
  echo "--- CSV ---"; cat "$RESULT_DIR/bench_${LABEL}_${IN}_${OUT}.csv"
}

run_one 2048 200
run_one 200 2048
