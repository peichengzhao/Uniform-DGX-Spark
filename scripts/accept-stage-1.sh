#!/usr/bin/env bash

set -euo pipefail

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
test_dir=$(mktemp -d /tmp/uniform-dgxspark-stage1.XXXXXX)
port=18180
base_url="http://127.0.0.1:${port}"
api_pid=""

stop_api() {
  if test -n "${api_pid}" && kill -0 "${api_pid}" >/dev/null 2>&1; then
    kill -TERM "${api_pid}" >/dev/null 2>&1 || true
    wait "${api_pid}" 2>/dev/null || true
  fi
  api_pid=""
}

cleanup() {
  stop_api
  if [[ "${test_dir}" == /tmp/uniform-dgxspark-stage1.* ]]; then
    rm -rf "${test_dir}"
  fi
}

trap cleanup EXIT
cd "${project_dir}"

for command_name in uv curl jq; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    printf 'FAIL: required command not found: %s\n' "${command_name}"
    exit 1
  fi
done

wait_for_api() {
  for attempt in $(seq 1 40); do
    if curl -fsS --max-time 2 "${base_url}/api/v1/health" >/dev/null 2>&1; then
      printf '      API ready after attempt %s.\n' "${attempt}"
      return 0
    fi
    if ! kill -0 "${api_pid}" >/dev/null 2>&1; then
      printf 'FAIL: API process exited during startup.\n'
      sed -n '1,160p' "${test_dir}/api.log"
      return 1
    fi
    sleep 1
  done
  printf 'FAIL: API did not become healthy within 40 seconds.\n'
  sed -n '1,160p' "${test_dir}/api.log"
  return 1
}

start_api() {
  UNIFORM_DATA_DIR="${test_dir}/data" \
    UNIFORM_CATALOG_AUTO_REFRESH=false \
    uv run uvicorn uniform_dgxspark.main:app \
      --host 127.0.0.1 --port "${port}" \
      >"${test_dir}/api.log" 2>&1 &
  api_pid=$!
  wait_for_api
}

printf '[1/8] Running automated tests...\n'
uv run pytest -q -W error

printf '[2/8] Starting the API with an isolated SQLite database...\n'
start_api

printf '[3/8] Checking health and database integrity...\n'
health=$(curl -fsS "${base_url}/api/v1/health")
test "$(jq -r '.status' <<<"${health}")" = "ok"
test "$(jq -r '.database' <<<"${health}")" = "ok"

printf '[4/8] Creating and reading a queued deployment...\n'
created=$(
  curl -fsS "${base_url}/api/v1/deployments" \
    -H 'Content-Type: application/json' \
    -d '{"model_id":"Qwen/Qwen2.5-0.5B-Instruct","revision":"stage-1-test","parameters":{"port":18080,"max_model_len":2048,"gpu_memory_utilization":0.8}}'
)
deployment_id=$(jq -r '.id' <<<"${created}")
test -n "${deployment_id}"
test "$(jq -r '.status' <<<"${created}")" = "queued"

fetched=$(curl -fsS "${base_url}/api/v1/deployments/${deployment_id}")
test "$(jq -r '.model_id' <<<"${fetched}")" = "Qwen/Qwen2.5-0.5B-Instruct"

listed=$(curl -fsS "${base_url}/api/v1/deployments")
test "$(jq -r '.total' <<<"${listed}")" = "1"

printf '[5/8] Cancelling the deployment and checking its event timeline...\n'
cancelled=$(curl -fsS -X POST "${base_url}/api/v1/deployments/${deployment_id}/cancel")
test "$(jq -r '.status' <<<"${cancelled}")" = "cancelled"

events=$(curl -fsS "${base_url}/api/v1/deployments/${deployment_id}/events")
test "$(jq -r '.items | length' <<<"${events}")" = "2"
test "$(jq -r '.items[0].event_type' <<<"${events}")" = "created"
test "$(jq -r '.items[1].event_type' <<<"${events}")" = "cancelled"

printf '[6/8] Restarting the API process...\n'
stop_api
start_api

printf '[7/8] Verifying SQLite persistence after restart...\n'
persisted=$(curl -fsS "${base_url}/api/v1/deployments/${deployment_id}")
test "$(jq -r '.status' <<<"${persisted}")" = "cancelled"
test "$(jq -r '.revision' <<<"${persisted}")" = "stage-1-test"

printf '[8/8] Stopping the API and cleaning isolated test data...\n'
stop_api

printf 'PASS: stage 1 acceptance checks succeeded.\n'
