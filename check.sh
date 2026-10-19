#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common_env.sh"
court_select_python
cd "$court_workspace"
exec "$court_python" -B -m unittest discover -s tests -p 'test_*.py' -v
