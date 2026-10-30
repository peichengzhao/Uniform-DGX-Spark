#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"

uv run pytest -q -W error tests/test_cluster_monitoring.py tests/test_monitoring.py
uv run ruff check \
  src/uniform_dgxspark/services/cluster_monitoring.py \
  src/uniform_dgxspark/services/node_probe.py \
  tests/test_cluster_monitoring.py
uv run python -m compileall -q src tests
node --check src/uniform_dgxspark/web/app.js
uv run python -m uniform_dgxspark.services.node_probe >/dev/null
rg -Fq 'memory_allocation_visible' src/uniform_dgxspark/services/node_probe.py
rg -Fq 'docker", "stats"' src/uniform_dgxspark/services/node_probe.py
rg -Fq 'GPU 显存 / 统一内存池' src/uniform_dgxspark/web/app.js
rg -Fq '容器内存' src/uniform_dgxspark/web/index.html

printf 'PASS: stage 31 multi-node resource monitoring passed.\n'
