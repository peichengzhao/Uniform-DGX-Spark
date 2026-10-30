#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"

uv run pytest -q -W error tests/test_ai_terminal.py
uv run ruff check src/uniform_dgxspark/schemas.py \
  src/uniform_dgxspark/main.py src/uniform_dgxspark/services/ai_terminal.py \
  tests/test_ai_terminal.py
node --check src/uniform_dgxspark/web/app.js
rg -q 'AI 一键部署' src/uniform_dgxspark/web/app.js
rg -q 'uniformctl deploy' src/uniform_dgxspark/services/ai_terminal.py
rg -q '/actions/deploy' src/uniform_dgxspark/main.py

printf 'PASS: stage 16 scoped AI one-click deployment checks succeeded.\n'
