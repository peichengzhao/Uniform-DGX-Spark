#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"

uv run pytest -q -W error tests/test_ai_terminal.py tests/test_docker_runtime.py
uv run ruff check src/uniform_dgxspark/ai_store.py \
  src/uniform_dgxspark/database.py src/uniform_dgxspark/main.py \
  src/uniform_dgxspark/schemas.py src/uniform_dgxspark/services/ai_terminal.py \
  src/uniform_dgxspark/services/docker_runtime.py \
  tests/test_ai_terminal.py tests/test_docker_runtime.py
uv run python -m compileall -q src tests
node --check src/uniform_dgxspark/web/app.js
rg -Fq 'RESULT_SCHEMA.md' src/uniform_dgxspark/services/ai_terminal.py
rg -Fq 'id="ai-deployment-result"' src/uniform_dgxspark/web/index.html
rg -Fq 'actions/result/managed' src/uniform_dgxspark/main.py

printf 'PASS: stage 19 unified AI deployment result passed.\n'
