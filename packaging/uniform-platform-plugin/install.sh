#!/usr/bin/env sh
# Convenience wrapper for Linux and macOS. All real work is in install.py.
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=${PYTHON:-python3}

exec "$PYTHON" "$HERE/install.py" "$@"
