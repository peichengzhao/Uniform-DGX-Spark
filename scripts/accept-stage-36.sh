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

.venv/bin/python -m pytest -q tests/test_evalscope_compatibility.py
.venv/bin/python -m ruff check \
  scripts/probe-evalscope-accuracy.py \
  tests/test_evalscope_compatibility.py
.venv/bin/python scripts/probe-evalscope-accuracy.py validate \
  --report tests/fixtures/evalscope/gsm8k-report-v2.json \
  --model stage-36-fixture-model >/dev/null

printf 'PASS: stage 36 EvalScope compatibility probe checks succeeded.\n'
