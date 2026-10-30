#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
test_dir=$(mktemp -d /tmp/uniform-dgxspark-stage7.XXXXXX)
port=18117
api_key=uniform-stage7-test-key
api_pid=""
cleanup(){ test -z "${api_pid}" || kill -TERM "${api_pid}" >/dev/null 2>&1 || true; test -z "${api_pid}" || wait "${api_pid}" 2>/dev/null || true; [[ "${test_dir}" == /tmp/uniform-dgxspark-stage7.* ]] && rm -rf "${test_dir}"; }
trap cleanup EXIT
cd "${project_dir}"
uv run pytest -q -W error tests/test_security.py tests/test_modelscope_downloads.py
UNIFORM_DATA_DIR="${test_dir}/data" UNIFORM_WORKER_ENABLED=false UNIFORM_API_TOKEN="${api_key}" uv run uvicorn uniform_dgxspark.main:app --host 127.0.0.1 --port "${port}" >"${test_dir}/api.log" 2>&1 & api_pid=$!
for attempt in $(seq 1 30); do curl -fsS "http://127.0.0.1:${port}/api/v1/auth/status" >/dev/null 2>&1 && break; sleep 1; done
test "$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}/api/v1/health")" = "401"
curl -fsS -H "X-API-Key: ${api_key}" "http://127.0.0.1:${port}/api/v1/health" | jq -e '.status == "ok"' >/dev/null
curl -fsS -X POST -H "X-API-Key: ${api_key}" "http://127.0.0.1:${port}/api/v1/catalog/refresh?source=modelscope&limit=5" | jq -e '.refreshed == 5' >/dev/null
curl -fsS -H "X-API-Key: ${api_key}" "http://127.0.0.1:${port}/api/v1/catalog/models?source=modelscope" | jq -e '.total == 5' >/dev/null
test -x scripts/run-local.sh
test -x scripts/install-user-service.sh
rg -q '@PROJECT_DIR@' packaging/uniform-dgxspark.service.in
printf 'PASS: stage 7 acceptance checks succeeded.\n'

