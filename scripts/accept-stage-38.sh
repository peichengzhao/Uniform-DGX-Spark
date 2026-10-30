#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"

if [[ ! -x .venv/bin/python || ! -x .venv/bin/ruff ]]; then
  uv_command=uv
  if ! command -v "${uv_command}" >/dev/null 2>&1 && [[ -x .venv/bin/uv ]]; then
    uv_command=.venv/bin/uv
  fi
  export UV_CACHE_DIR="${UV_CACHE_DIR:-${project_dir}/.runtime/uv-cache}"
  "${uv_command}" sync --frozen --offline --group dev
fi

.venv/bin/python -m pytest -q \
  tests/test_accuracy_store.py \
  tests/test_model_targets.py \
  tests/test_evalscope_accuracy.py \
  tests/test_benchmarking.py \
  tests/test_arena.py
.venv/bin/python -m ruff check \
  src/uniform_dgxspark/accuracy_store.py \
  src/uniform_dgxspark/services/model_targets.py \
  src/uniform_dgxspark/services/evalscope_accuracy.py \
  src/uniform_dgxspark/services/accuracy.py \
  src/uniform_dgxspark/services/benchmarking.py \
  src/uniform_dgxspark/services/arena.py \
  tests/test_accuracy_store.py \
  tests/test_model_targets.py \
  tests/test_evalscope_accuracy.py
.venv/bin/python -m compileall -q src tests

printf 'PASS: stage 38 EvalScope execution and normalization checks succeeded.\n'
