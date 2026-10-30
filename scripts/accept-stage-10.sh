#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"
uv run pytest -q -W error tests/test_ai_terminal.py::test_codex_and_claude_use_approval_modes
rg -q 'Claude Code' src/uniform_dgxspark/web/index.html
printf 'PASS: stage 10 Claude Code adapter and availability checks succeeded.\n'
