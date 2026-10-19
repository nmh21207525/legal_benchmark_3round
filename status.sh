#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common_env.sh"
court_select_python
exec "$court_python" -B "$court_workspace/status.py" "$@"
