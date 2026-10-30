#!/usr/bin/env bash

set -euo pipefail

container="uniform-dgxspark-phase0-vllm"
base_url="http://127.0.0.1:18000"

stop_container() {
  docker stop --timeout 20 "${container}" >/dev/null 2>&1 || true
}

trap stop_container EXIT

for command_name in docker curl jq rg; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    printf 'FAIL: required command not found: %s\n' "${command_name}"
    exit 1
  fi
done

if ! docker inspect "${container}" >/dev/null 2>&1; then
  printf 'FAIL: test container does not exist: %s\n' "${container}"
  exit 1
fi

printf '[1/6] Starting the phase 0 test container...\n'
docker start "${container}" >/dev/null

printf '[2/6] Waiting for vLLM health check...\n'
ready=false
for attempt in $(seq 1 40); do
  if curl -fsS --max-time 2 "${base_url}/health" >/dev/null 2>&1; then
    ready=true
    printf '      Ready after attempt %s.\n' "${attempt}"
    break
  fi

  state=$(docker inspect "${container}" --format '{{.State.Status}}')
  if test "${state}" = "exited"; then
    printf 'FAIL: container exited before becoming healthy.\n'
    docker logs --tail 100 "${container}"
    exit 1
  fi
  sleep 2
done

if test "${ready}" != "true"; then
  printf 'FAIL: vLLM did not become healthy within 80 seconds.\n'
  docker logs --tail 100 "${container}"
  exit 1
fi

printf '[3/6] Checking the OpenAI model list...\n'
model_id=$(curl -fsS "${base_url}/v1/models" | jq -r '.data[0].id')
if test "${model_id}" != "phase0-qwen"; then
  printf 'FAIL: expected model phase0-qwen, got %s.\n' "${model_id}"
  exit 1
fi

printf '[4/6] Running an OpenAI-compatible completion...\n'
completion_tokens=$(
  curl -fsS "${base_url}/v1/completions" \
    -H 'Content-Type: application/json' \
    -d '{"model":"phase0-qwen","prompt":"test","max_tokens":2,"temperature":0}' \
    | jq -r '.usage.completion_tokens'
)
if test "${completion_tokens}" != "2"; then
  printf 'FAIL: expected 2 completion tokens, got %s.\n' "${completion_tokens}"
  exit 1
fi

printf '[5/6] Checking vLLM Prometheus metrics...\n'
if ! curl -fsS "${base_url}/metrics" | rg -q '^vllm:request_success_total'; then
  printf 'FAIL: vLLM request metrics were not found.\n'
  exit 1
fi

printf '[6/6] Stopping the temporary test container...\n'
stop_container
trap - EXIT

exit_code=$(docker inspect "${container}" --format '{{.State.ExitCode}}')
state=$(docker inspect "${container}" --format '{{.State.Status}}')
if test "${state}" != "exited" || test "${exit_code}" != "0"; then
  printf 'FAIL: unexpected final container state=%s exit=%s.\n' "${state}" "${exit_code}"
  exit 1
fi

printf 'PASS: stage 0 acceptance checks succeeded.\n'

