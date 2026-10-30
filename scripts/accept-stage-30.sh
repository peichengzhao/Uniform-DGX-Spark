#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"

uv run pytest -q -W error \
  tests/test_ai_terminal.py::test_ai_sessions_filter_and_resume_existing_model_context \
  tests/test_ai_terminal.py::test_ai_session_creation_is_idempotent \
  tests/test_ai_terminal.py::test_ai_session_archive_restore_and_delete
uv run ruff check \
  src/uniform_dgxspark/schemas.py \
  src/uniform_dgxspark/ai_store.py \
  src/uniform_dgxspark/database.py \
  src/uniform_dgxspark/main.py \
  src/uniform_dgxspark/services/ai_terminal.py \
  tests/test_ai_terminal.py
uv run python -m compileall -q src tests
node --check src/uniform_dgxspark/web/app.js
rg -Fq 'target_model_id=${encodeURIComponent(model.model)}' src/uniform_dgxspark/web/app.js
rg -Fq '/api/v1/ai/sessions/{session_id}/resume' src/uniform_dgxspark/main.py
rg -Fq '发现该模型的部署会话' src/uniform_dgxspark/web/index.html

printf 'PASS: stage 30 AI deployment session reuse passed.\n'
