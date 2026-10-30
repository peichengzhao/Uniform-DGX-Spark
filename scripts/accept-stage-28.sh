#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"

uv run pytest -q -W error \
  tests/test_ai_terminal.py::test_existing_ai_session_gets_compatible_name_and_target \
  tests/test_ai_terminal.py::test_ai_session_creation_is_idempotent
uv run ruff check \
  src/uniform_dgxspark/database.py \
  src/uniform_dgxspark/schemas.py \
  src/uniform_dgxspark/ai_store.py \
  src/uniform_dgxspark/services/ai_terminal.py \
  src/uniform_dgxspark/main.py \
  tests/test_ai_terminal.py
uv run python -m compileall -q src tests
node --check src/uniform_dgxspark/web/app.js
rg -Fq 'client_request_id' src/uniform_dgxspark/web/app.js
rg -Fq '正在创建 AI 会话' src/uniform_dgxspark/web/app.js
rg -Fq 'idx_ai_sessions_client_request' src/uniform_dgxspark/database.py

printf 'PASS: stage 28 immediate AI launch feedback and idempotency passed.\n'
