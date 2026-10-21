#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
source ./common_env.sh
LEGAL_COMMAND="${1:-background}"
if [[ $# -gt 0 ]]; then shift; fi
case "$LEGAL_COMMAND" in
  background|pipeline|preflight|resume|grade) legal_require_api_keys ;;
esac
bash run_005.sh "$LEGAL_COMMAND" "$@"
bash run_006.sh "$LEGAL_COMMAND" "$@"
