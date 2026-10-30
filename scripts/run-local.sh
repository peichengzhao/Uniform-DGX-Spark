#!/usr/bin/env bash

set -euo pipefail

project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
bind_host=${UNIFORM_HOST:-127.0.0.1}
bind_port=${UNIFORM_PORT:-18180}
data_dir=${UNIFORM_DATA_DIR:-"${project_dir}/data"}

cd "${project_dir}"
uv sync --frozen
exec env UNIFORM_DATA_DIR="${data_dir}" \
  uv run python -m uvicorn uniform_dgxspark.main:app \
  --host "${bind_host}" --port "${bind_port}"
