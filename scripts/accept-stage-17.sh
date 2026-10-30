#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"

uv run pytest -q -W error tests/test_local_models.py
uv run ruff check src/uniform_dgxspark/services/local_models.py \
  src/uniform_dgxspark/main.py src/uniform_dgxspark/schemas.py \
  tests/test_local_models.py
node --check src/uniform_dgxspark/web/app.js
rg -q 'data-view="local-models"' src/uniform_dgxspark/web/index.html
rg -q '/api/v1/local-models' src/uniform_dgxspark/main.py

printf 'PASS: stage 17 configurable local model inventory checks succeeded.\n'
