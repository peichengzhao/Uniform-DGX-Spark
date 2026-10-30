#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"
uv run pytest -q -W error tests/test_ai_terminal.py::test_ai_environment_does_not_forward_platform_secrets tests/test_security.py
rg -q '不接受、不保存 sudo 密码' docs/USER_GUIDE.md
printf 'PASS: stage 11 approval, credential isolation and sudo policy checks succeeded.\n'
