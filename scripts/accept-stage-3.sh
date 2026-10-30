#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"
printf '[1/2] Running resumable cache unit tests...\n'
uv run pytest -q -W error tests/test_downloads.py tests/test_modelscope_downloads.py
printf '[2/2] Downloading a small pinned Hugging Face snapshot twice...\n'
uv run python scripts/accept_stage_3.py
printf 'PASS: stage 3 acceptance checks succeeded.\n'

