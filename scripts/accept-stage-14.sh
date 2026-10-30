#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
test_dir=$(mktemp -d /tmp/uniform-dgxspark-stage14.XXXXXX)
port=18124
api_pid=""
cleanup(){ test -z "${api_pid}" || kill -TERM "${api_pid}" >/dev/null 2>&1 || true; test -z "${api_pid}" || wait "${api_pid}" 2>/dev/null || true; [[ "${test_dir}" == /tmp/uniform-dgxspark-stage14.* ]] && rm -rf "${test_dir}"; }
trap cleanup EXIT
cd "${project_dir}"
uv run pytest -q -W error tests/test_catalog.py tests/test_catalog_refresh_worker.py
UNIFORM_DATA_DIR="${test_dir}/data" UNIFORM_WORKER_ENABLED=false \
  uv run uvicorn uniform_dgxspark.main:app --host 127.0.0.1 --port "${port}" \
  >"${test_dir}/api.log" 2>&1 &
api_pid=$!
for attempt in $(seq 1 30); do
  curl -fsS "http://127.0.0.1:${port}/api/v1/health" >/dev/null 2>&1 && break
  sleep 1
done
result=$(curl -fsS --max-time 30 "http://127.0.0.1:${port}/api/v1/catalog/search?query=Qwen2.5&source=all&limit=5")
jq -e '.live == true and .total > 0 and (.items | length) > 0' <<<"${result}" >/dev/null
jq -e '.source_errors == {}' <<<"${result}" >/dev/null
printf 'PASS: stage 14 live website search and background refresh checks succeeded.\n'
