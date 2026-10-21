#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
LEGAL_PYTHON="${PYTHON_BIN:-.venv/bin/python}"
if [[ ! -x "$LEGAL_PYTHON" ]]; then LEGAL_PYTHON=python3; fi
exec "$LEGAL_PYTHON" -m shared_engine.cli export-all --case 005 "$@"
