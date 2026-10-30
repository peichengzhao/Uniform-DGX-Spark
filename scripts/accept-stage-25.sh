#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"

uv run pytest -q -W error tests/test_cluster_monitoring.py tests/test_monitoring.py
uv run ruff check \
  src/uniform_dgxspark/services/node_probe.py \
  tests/test_cluster_monitoring.py
uv run python -m compileall -q src tests
node --check src/uniform_dgxspark/web/app.js
uv run python -m uniform_dgxspark.services.node_probe >/dev/null
rg -Fq '仅物理链路' src/uniform_dgxspark/web/app.js
rg -Fq '网络已配置' src/uniform_dgxspark/web/app.js
if rg -Fq '已连接 (ACTIVE)' src/uniform_dgxspark/web/app.js; then
  printf 'FAIL: ambiguous RDMA connected label still exists.\n' >&2
  exit 1
fi

printf 'PASS: stage 25 RDMA link status semantics passed.\n'
