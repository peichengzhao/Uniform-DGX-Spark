#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"

uv run pytest -q -W error \
  tests/test_ai_terminal.py::test_ai_session_auto_resumes_after_rate_limit_and_honors_retry_limit
uv run ruff check \
  src/uniform_dgxspark/schemas.py \
  src/uniform_dgxspark/main.py \
  src/uniform_dgxspark/services/ai_terminal.py \
  tests/test_ai_terminal.py
uv run python -m compileall -q src tests
node --check src/uniform_dgxspark/web/app.js
rg -Fq '自动续跑' src/uniform_dgxspark/web/index.html
rg -Fq 'configure_ai_auto_resume' src/uniform_dgxspark/main.py

printf 'PASS: stage 27 AI interruption auto-resume passed.\n'
