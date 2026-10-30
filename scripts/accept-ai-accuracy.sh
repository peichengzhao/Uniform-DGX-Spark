#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"
if [[ -x "${project_dir}/.runtime/node-v22.23.3-linux-arm64/bin/node" ]]; then
  export PATH="${project_dir}/.runtime/node-v22.23.3-linux-arm64/bin:${PATH}"
fi
node --check src/uniform_dgxspark/web/app.js
.venv/bin/python -m ruff check src tests scripts/stage_41_live_acceptance.py
.venv/bin/python -m compileall -q src tests
UNIFORM_REQUIRE_BROWSER=1 UNIFORM_REQUIRE_CODEX=1 .venv/bin/python -m pytest -q \
  tests/test_accuracy_codex_approval.py \
  tests/test_ai_accuracy.py tests/test_ai_accuracy_browser.py \
  tests/test_ai_terminal.py tests/test_accuracy_api.py tests/test_accuracy_identity.py \
  tests/test_accuracy_store.py tests/test_accuracy_ui.py tests/test_accuracy_browser.py
printf 'PASS: AI accuracy orchestration, real Codex approvals, scoped MCP and browser regression.\n'
