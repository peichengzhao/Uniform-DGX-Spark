#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"
node --check src/uniform_dgxspark/web/app.js
rg -q 'diagnose-with-ai' src/uniform_dgxspark/web/app.js
rg -q 'deployment-context.json' src/uniform_dgxspark/services/ai_terminal.py
printf 'PASS: stage 12 deployment-linked AI diagnosis checks succeeded.\n'
