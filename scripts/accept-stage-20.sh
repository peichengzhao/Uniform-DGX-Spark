#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"

uv run pytest -q -W error tests/test_cluster_monitoring.py tests/test_monitoring.py tests/test_api.py
uv run ruff check src/uniform_dgxspark/node_store.py \
  src/uniform_dgxspark/services/cluster_monitoring.py \
  src/uniform_dgxspark/services/node_probe.py tests/test_cluster_monitoring.py
uv run python -m compileall -q src tests
node --check src/uniform_dgxspark/web/app.js
uv run python -m uniform_dgxspark.services.node_probe >/dev/null
rg -Fq 'id="cluster-nodes"' src/uniform_dgxspark/web/index.html
rg -Fq 'StrictHostKeyChecking=yes' src/uniform_dgxspark/services/cluster_monitoring.py

printf 'PASS: stage 20 dynamic SSH and CX-7 monitoring passed.\n'
