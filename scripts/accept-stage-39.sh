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

./scripts/accept-stage-38.sh
.venv/bin/python -m pytest -q \
  tests/test_accuracy_api.py \
  tests/test_security.py \
  tests/test_state_and_recovery.py
.venv/bin/python -m ruff check \
  src/uniform_dgxspark/config.py \
  src/uniform_dgxspark/schemas.py \
  src/uniform_dgxspark/accuracy_store.py \
  src/uniform_dgxspark/services/accuracy.py \
  src/uniform_dgxspark/main.py \
  tests/test_accuracy_api.py
.venv/bin/python -m compileall -q src tests

printf 'PASS: stage 39 Accuracy lifecycle and HTTP API checks succeeded.\n'
