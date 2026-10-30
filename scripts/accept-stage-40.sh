#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"

./scripts/accept-stage-39.sh
if [[ -x "${project_dir}/.runtime/node-v22.23.3-linux-arm64/bin/node" ]]; then
  export PATH="${project_dir}/.runtime/node-v22.23.3-linux-arm64/bin:${PATH}"
fi
node --check src/uniform_dgxspark/web/app.js
UNIFORM_REQUIRE_BROWSER=1 .venv/bin/python -m pytest -q \
  tests/test_accuracy_ui.py \
  tests/test_accuracy_browser.py \
  tests/test_api.py
.venv/bin/python -m ruff check src tests
.venv/bin/python -m compileall -q src tests

printf 'PASS: stage 40 Accuracy web workspace and real browser checks succeeded.\n'
