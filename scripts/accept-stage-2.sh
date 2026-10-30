#!/usr/bin/env bash

set -euo pipefail

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
test_dir=$(mktemp -d /tmp/uniform-dgxspark-stage2.XXXXXX)
model_dir=/tmp/uniform-dgxspark-phase0-local-qwen
api_port=18112
model_port=18012
api_pid=""
deployment_id=""

cleanup() {
  if test -n "${api_pid}" && kill -0 "${api_pid}" >/dev/null 2>&1; then
    kill -TERM "${api_pid}" >/dev/null 2>&1 || true
    wait "${api_pid}" 2>/dev/null || true
  fi
  if test -n "${deployment_id}"; then
    container_name="uniform-vllm-${deployment_id//-/}"
    container_name=${container_name:0:29}
    if test "$(docker inspect --format '{{ index .Config.Labels "com.uniform-dgxspark.deployment-id" }}' "${container_name}" 2>/dev/null || true)" = "${deployment_id}"; then
      docker rm -f "${container_name}" >/dev/null 2>&1 || true
    fi
  fi
  [[ "${test_dir}" == /tmp/uniform-dgxspark-stage2.* ]] && rm -rf "${test_dir}"
}
trap cleanup EXIT

cd "${project_dir}"
if ! test -f "${model_dir}/config.json"; then
  printf 'FAIL: stage 0 test model is missing: %s\n' "${model_dir}"
  exit 1
fi

printf '[1/7] Running worker and Docker unit tests...\n'
uv run pytest -q -W error tests/test_worker.py tests/test_docker_runtime.py

printf '[2/7] Starting isolated control API...\n'
UNIFORM_DATA_DIR="${test_dir}/data" UNIFORM_STARTUP_TIMEOUT_SECONDS=180 \
  UNIFORM_CATALOG_AUTO_REFRESH=false \
  uv run uvicorn uniform_dgxspark.main:app --host 127.0.0.1 --port "${api_port}" \
  >"${test_dir}/api.log" 2>&1 &
api_pid=$!
for attempt in $(seq 1 30); do
  curl -fsS --max-time 2 "http://127.0.0.1:${api_port}/api/v1/health" >/dev/null 2>&1 && break
  sleep 1
done

printf '[3/7] Creating a real vLLM deployment...\n'
created=$(curl -fsS "http://127.0.0.1:${api_port}/api/v1/deployments" \
  -H 'Content-Type: application/json' \
  -d "{\"model_id\":\"stage2-qwen\",\"parameters\":{\"port\":${model_port},\"served_model_name\":\"stage2-qwen\",\"local_model_path\":\"${model_dir}\",\"max_model_len\":256,\"gpu_memory_utilization\":0.02,\"startup_timeout_seconds\":180,\"extra_args\":[\"--enforce-eager\"]}}")
deployment_id=$(jq -r '.id' <<<"${created}")

printf '[4/7] Waiting for load and inference smoke test...\n'
for attempt in $(seq 1 150); do
  task=$(curl -fsS "http://127.0.0.1:${api_port}/api/v1/deployments/${deployment_id}")
  task_status=$(jq -r '.status' <<<"${task}")
  printf '      %-3s %s %s%%\n' "${attempt}" "${task_status}" "$(jq -r '.progress * 100 | floor' <<<"${task}")"
  test "${task_status}" = "running" && break
  if test "${task_status}" = "failed" -o "${task_status}" = "cancelled"; then
    jq . <<<"${task}"
    curl -sS "http://127.0.0.1:${api_port}/api/v1/deployments/${deployment_id}/logs" | jq -r '.logs' || true
    exit 1
  fi
  sleep 2
done
test "${task_status}" = "running"

printf '[5/7] Calling the deployed OpenAI-compatible endpoint...\n'
completion=$(curl -fsS "http://127.0.0.1:${model_port}/v1/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"stage2-qwen","prompt":"test","max_tokens":2,"temperature":0}')
test "$(jq -r '.usage.completion_tokens' <<<"${completion}")" = "2"

printf '[6/7] Checking logs, events, and runtime metrics...\n'
test "$(curl -fsS "http://127.0.0.1:${api_port}/api/v1/deployments/${deployment_id}/events" | jq -r '.items[-1].to_status')" = "running"
curl -fsS "http://127.0.0.1:${api_port}/api/v1/deployments/${deployment_id}/logs" | jq -e '.logs | length > 0' >/dev/null
curl -fsS "http://127.0.0.1:${api_port}/api/v1/deployments/${deployment_id}/metrics" | jq -e '.vllm | length > 0' >/dev/null

printf '[7/7] Stopping the deployment through the API...\n'
curl -fsS -X POST "http://127.0.0.1:${api_port}/api/v1/deployments/${deployment_id}/stop" >/dev/null
for attempt in $(seq 1 30); do
  task_status=$(curl -fsS "http://127.0.0.1:${api_port}/api/v1/deployments/${deployment_id}" | jq -r '.status')
  test "${task_status}" = "stopped" && break
  sleep 1
done
test "${task_status}" = "stopped"

printf 'PASS: stage 2 acceptance checks succeeded.\n'
