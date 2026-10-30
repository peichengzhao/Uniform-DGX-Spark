#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"

uv run pytest -q -W error tests/test_ai_terminal.py tests/test_api.py
uv run ruff check \
  src/uniform_dgxspark/database.py \
  src/uniform_dgxspark/schemas.py \
  src/uniform_dgxspark/ai_store.py \
  src/uniform_dgxspark/services/ai_terminal.py \
  tests/test_ai_terminal.py \
  tests/test_api.py
uv run python -m compileall -q src tests
node --check src/uniform_dgxspark/web/app.js
rg -Fq 'name="display_name"' src/uniform_dgxspark/web/index.html
rg -Fq 'name="ai_model"' src/uniform_dgxspark/web/index.html
rg -Fq 'name="reasoning_effort"' src/uniform_dgxspark/web/index.html
rg -Fq '显示已存档' src/uniform_dgxspark/web/index.html
rg -Fq 'delete_ai_session' src/uniform_dgxspark/main.py

printf 'PASS: stage 26 AI session identity and lifecycle controls passed.\n'
