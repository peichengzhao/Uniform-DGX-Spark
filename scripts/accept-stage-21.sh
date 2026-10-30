#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"

uv run pytest -q -W error \
  tests/test_catalog.py \
  tests/test_catalog_api.py \
  tests/test_publisher_icons.py \
  tests/test_catalog_refresh_worker.py \
  tests/test_security.py
uv run ruff check \
  src/uniform_dgxspark/catalog.py \
  src/uniform_dgxspark/services/publisher_icons.py \
  tests/test_catalog.py \
  tests/test_catalog_api.py \
  tests/test_publisher_icons.py
uv run python -m compileall -q src tests
node --check src/uniform_dgxspark/web/app.js
rg -Fq 'publisher-avatar' src/uniform_dgxspark/web/app.css

printf 'PASS: stage 21 unified model catalog passed.\n'
