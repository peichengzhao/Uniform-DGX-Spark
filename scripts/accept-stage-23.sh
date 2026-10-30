#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"

uv run pytest -q -W error tests/test_catalog.py tests/test_catalog_api.py
uv run ruff check \
  src/uniform_dgxspark/catalog.py \
  tests/test_catalog.py
uv run python -m compileall -q src tests
node --check src/uniform_dgxspark/web/app.js
rg -Fq '权重下载' src/uniform_dgxspark/web/app.js
rg -Fq '仓库总量' src/uniform_dgxspark/web/app.js
rg -Fq '量化/精度' src/uniform_dgxspark/web/app.js
rg -Fq '模型结构' src/uniform_dgxspark/web/app.js
rg -Fq '参数量' src/uniform_dgxspark/web/app.js

printf 'PASS: stage 23 model card metadata passed.\n'
