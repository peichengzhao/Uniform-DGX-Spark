#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"
uv run pytest -q -W error tests/test_ai_terminal.py::test_reference_fetcher_blocks_local_network
test -f src/uniform_dgxspark/web/vendor/node_modules/@xterm/xterm/lib/xterm.mjs
rg -q 'reference_urls' src/uniform_dgxspark/web/app.js
printf 'PASS: stage 9 Codex, URL reference and local xterm checks succeeded.\n'
