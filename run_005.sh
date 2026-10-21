#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
source ./common_env.sh
LEGAL_PYTHON="${PYTHON_BIN:-.venv/bin/python}"
if [[ ! -x "$LEGAL_PYTHON" ]]; then LEGAL_PYTHON=python3; fi
LEGAL_COMMAND="${1:-background}"
if [[ $# -gt 0 ]]; then shift; fi
case "$LEGAL_COMMAND" in
  background|pipeline|preflight|resume|grade) legal_require_api_keys ;;
esac
exec "$LEGAL_PYTHON" -m shared_engine.cli "$LEGAL_COMMAND" --case 005 "$@"
