#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"
uv run pytest -q -W error tests/test_docker_runtime.py
node --check src/uniform_dgxspark/web/app.js
rg -Fq 'id="image-sort"' src/uniform_dgxspark/web/index.html
rg -Fq 'sorting.startsWith("size")' src/uniform_dgxspark/web/app.js
rg -Fq 'sorting.startsWith("time")' src/uniform_dgxspark/web/app.js
if rg -q 'class="image-card"' src/uniform_dgxspark/web/app.js; then
  printf 'FAIL: legacy image cards are still rendered.\n' >&2
  exit 1
fi
docker image ls --format '{{json .}}' | jq -s -e 'length >= 1 and .[0].Repository != null' >/dev/null
curl -fsS http://127.0.0.1:18180/api/v1/images | jq -e '.total >= 1 and (.items | length) >= 1' >/dev/null
printf 'PASS: stage 15 sortable local image list and deployment selection checks succeeded.\n'
