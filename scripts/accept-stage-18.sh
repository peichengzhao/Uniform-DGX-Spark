#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"

uv run pytest -q -W error tests/test_ai_terminal.py
uv run ruff check src/uniform_dgxspark/ai_store.py \
  src/uniform_dgxspark/database.py src/uniform_dgxspark/schemas.py \
  src/uniform_dgxspark/services/ai_terminal.py tests/test_ai_terminal.py
node --check src/uniform_dgxspark/web/app.js
rg -Fq 'value="autonomous"' src/uniform_dgxspark/web/index.html
rg -Fq 'value="full_access"' src/uniform_dgxspark/web/index.html
rg -Fq 'approvals_reviewer="auto_review"' src/uniform_dgxspark/services/ai_terminal.py
rg -Fq 'danger-full-access' src/uniform_dgxspark/services/ai_terminal.py

printf 'PASS: stage 18 autonomous AI deployment modes passed.\n'
