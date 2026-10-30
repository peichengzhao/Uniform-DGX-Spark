#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"
uv run pytest -q -W error tests/test_ai_terminal.py::test_ai_provider_status_and_session_transcript
rg -q 'AITerminalManager' src/uniform_dgxspark/main.py
rg -q 'websocket.*terminal' src/uniform_dgxspark/main.py
printf 'PASS: stage 8 PTY, persistence, reconnect and audit checks succeeded.\n'
