#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"
uv sync --frozen --group dev
uv run python -m pytest -q tests/test_ui_editing.py tests/test_api.py
uv run python -m ruff check \
  src/uniform_dgxspark/ui_edit_store.py \
  src/uniform_dgxspark/services/ui_editing.py \
  tests/test_ui_editing.py
node --check src/uniform_dgxspark/web/app.js
printf 'PASS: stage 35 visual UI editing checks succeeded.\n'
