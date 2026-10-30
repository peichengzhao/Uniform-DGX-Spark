#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
test_dir=$(mktemp -d /tmp/uniform-dgxspark-stage4.XXXXXX)
port=18114
api_pid=""
cleanup(){ test -z "${api_pid}" || kill -TERM "${api_pid}" >/dev/null 2>&1 || true; test -z "${api_pid}" || wait "${api_pid}" 2>/dev/null || true; [[ "${test_dir}" == /tmp/uniform-dgxspark-stage4.* ]] && rm -rf "${test_dir}"; }
trap cleanup EXIT
cd "${project_dir}"
UNIFORM_DATA_DIR="${test_dir}/data" UNIFORM_WORKER_ENABLED=false uv run uvicorn uniform_dgxspark.main:app --host 127.0.0.1 --port "${port}" >"${test_dir}/api.log" 2>&1 & api_pid=$!
for attempt in $(seq 1 30); do curl -fsS "http://127.0.0.1:${port}/" >/dev/null 2>&1 && break; sleep 1; done
printf '[1/4] Checking Web application shell...\n'
curl -fsS "http://127.0.0.1:${port}/" | rg -q 'id="deployment-list"'
printf '[2/4] Checking CSS and JavaScript assets...\n'
curl -fsS "http://127.0.0.1:${port}/static/app.css" | rg -q -- '--green'
curl -fsS "http://127.0.0.1:${port}/static/app.js" | rg -q 'EventSource'
printf '[3/4] Checking server-sent deployment events...\n'
set +e
stream=$(curl -sS --max-time 3 "http://127.0.0.1:${port}/api/v1/events/stream" 2>/dev/null)
set -e
rg -q 'event: deployments' <<<"${stream}"
printf '[4/4] Checking browser API routes...\n'
curl -fsS "http://127.0.0.1:${port}/api/v1/deployments" | jq -e '.total == 0' >/dev/null
printf 'PASS: stage 4 acceptance checks succeeded.\n'
