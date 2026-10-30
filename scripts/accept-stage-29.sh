#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"

uv run pytest -q -W error \
  tests/test_docker_runtime.py::test_logs_for_reported_container_validates_name_and_adds_timestamps \
  tests/test_api.py::test_managed_deployment_service_log_websocket
uv run ruff check \
  src/uniform_dgxspark/services/docker_runtime.py \
  src/uniform_dgxspark/main.py \
  tests/test_docker_runtime.py \
  tests/test_api.py
uv run python -m compileall -q src tests
node --check src/uniform_dgxspark/web/app.js
rg -Fq 'service-logs/{source}/{resource_id}' src/uniform_dgxspark/main.py
rg -Fq '实时服务日志' src/uniform_dgxspark/web/index.html
rg -Fq 'view-ai-service-logs' src/uniform_dgxspark/web/app.js

printf 'PASS: stage 29 live service logs passed.\n'
