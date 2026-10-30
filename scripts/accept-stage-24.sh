#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"

uv run pytest -q -W error tests/test_catalog.py tests/test_catalog_api.py
uv run ruff check \
  src/uniform_dgxspark/catalog.py \
  src/uniform_dgxspark/main.py \
  tests/test_catalog.py tests/test_catalog_api.py
uv run python -m compileall -q src tests
node --check src/uniform_dgxspark/web/app.js
rg -Fq '/api/v1/catalog/model-metadata' src/uniform_dgxspark/main.py
rg -Fq 'IntersectionObserver' src/uniform_dgxspark/web/app.js
rg -Fq 'weight_metadata_verified' src/uniform_dgxspark/catalog.py

printf 'PASS: stage 24 verified model weight sizes passed.\n'
