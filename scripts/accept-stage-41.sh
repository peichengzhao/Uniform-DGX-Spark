#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${project_dir}"

./scripts/accept-stage-40.sh
.venv/bin/python -m pytest -q tests/test_accuracy_identity.py
.venv/bin/python -m ruff check src tests scripts/stage_41_live_acceptance.py
.venv/bin/python -m compileall -q src tests scripts/stage_41_live_acceptance.py
printf 'PASS: stage 41 identity, isolated cache, comparison and recovery regression checks succeeded.\n'

if [[ "${1:-}" == "--live" ]]; then
  : "${UNIFORM_STAGE41_EVALSCOPE_COMMAND:?Set the pinned isolated EvalScope executable}"
  : "${UNIFORM_STAGE41_EVALSCOPE_SOURCE:?Set its pinned source checkout}"
  printf 'Starting live model acceptance against a registered loopback model; production storage/services remain unchanged.\n'
  PYTHONPATH=tests UNIFORM_REQUIRE_BROWSER=1 .venv/bin/python -m pytest -s -q \
    scripts/stage_41_live_acceptance.py
fi
