#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
test_dir=$(mktemp -d /tmp/uniform-dgxspark-stage5.XXXXXX)
port=18115
api_pid=""
cleanup(){ test -z "${api_pid}" || kill -TERM "${api_pid}" >/dev/null 2>&1 || true; test -z "${api_pid}" || wait "${api_pid}" 2>/dev/null || true; [[ "${test_dir}" == /tmp/uniform-dgxspark-stage5.* ]] && rm -rf "${test_dir}"; }
trap cleanup EXIT
cd "${project_dir}"
uv run pytest -q -W error tests/test_monitoring.py
UNIFORM_DATA_DIR="${test_dir}/data" UNIFORM_WORKER_ENABLED=false UNIFORM_METRICS_INTERVAL_SECONDS=1 uv run uvicorn uniform_dgxspark.main:app --host 127.0.0.1 --port "${port}" >"${test_dir}/api.log" 2>&1 & api_pid=$!
for attempt in $(seq 1 30); do curl -fsS "http://127.0.0.1:${port}/api/v1/health" >/dev/null 2>&1 && break; sleep 1; done
sleep 2
metrics=$(curl -fsS "http://127.0.0.1:${port}/api/v1/system/metrics?history=3")
jq -e '.cpu.logical_count >= 1 and .unified_memory.total_bytes > 0 and .disk.total_bytes > 0 and (.history|length) >= 2' <<<"${metrics}" >/dev/null
jq -e '.gpu.available == true and .gpu.name == "NVIDIA GB10"' <<<"${metrics}" >/dev/null
printf 'PASS: stage 5 acceptance checks succeeded.\n'

