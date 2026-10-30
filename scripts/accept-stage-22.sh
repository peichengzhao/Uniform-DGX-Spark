#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"

uv run pytest -q -W error tests/test_catalog.py tests/test_catalog_api.py
uv run ruff check \
  src/uniform_dgxspark/catalog.py \
  src/uniform_dgxspark/database.py \
  src/uniform_dgxspark/main.py \
  src/uniform_dgxspark/schemas.py \
  tests/test_catalog.py tests/test_catalog_api.py
uv run python -m compileall -q src tests
node --check src/uniform_dgxspark/web/app.js
rg -Fq 'id="catalog-sort"' src/uniform_dgxspark/web/index.html
rg -Fq '发布时间（最新）' src/uniform_dgxspark/web/index.html
rg -Fq '下载量（最多）' src/uniform_dgxspark/web/index.html
rg -Fq 'find(entry=>entry.source==="modelscope")' src/uniform_dgxspark/web/app.js
if rg -q '主来源|备用来源|联合模型库' \
  src/uniform_dgxspark/web/index.html src/uniform_dgxspark/web/app.js; then
  printf 'FAIL: internal catalog merge information is visible in the UI.\n' >&2
  exit 1
fi

printf 'PASS: stage 22 catalog sorting and Hugging Face display passed.\n'
