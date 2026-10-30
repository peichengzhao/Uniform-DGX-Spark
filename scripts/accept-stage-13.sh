#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"
uv run pytest -q -W error tests/test_streaming_commands.py
rg -q 'image_pull_output' src/uniform_dgxspark/services/worker.py
printf 'PASS: stage 13 streamed image pull and cancellation checks succeeded.\n'
