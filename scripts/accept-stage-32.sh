#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"

uv run pytest -q -W error tests/test_benchmarking.py
uv run ruff check \
  src/uniform_dgxspark/benchmark_store.py \
  src/uniform_dgxspark/services/benchmarking.py \
  src/uniform_dgxspark/database.py \
  src/uniform_dgxspark/schemas.py \
  src/uniform_dgxspark/main.py \
  tests/test_benchmarking.py
uv run python -m compileall -q src tests
node --check src/uniform_dgxspark/web/app.js
rg -Fq 'id="view-benchmarks"' src/uniform_dgxspark/web/index.html
rg -Fq '交给 AI 分析' src/uniform_dgxspark/web/app.js
rg -Fq '/api/v1/benchmarks/{benchmark_id}/cancel' src/uniform_dgxspark/main.py

printf 'PASS: stage 32 visual inference benchmarking passed.\n'
