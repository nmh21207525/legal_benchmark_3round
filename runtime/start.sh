#!/usr/bin/env bash
set -euo pipefail
court_package_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
court_python="${COURT_PYTHON:-/home/nmh/legal_agent/.venv/bin/python}"
if [[ ! -x "$court_python" ]]; then
  court_python="$(command -v python3 || true)"
fi
if [[ -z "$court_python" || ! -x "$court_python" ]]; then
  echo "需要 Python 3.10+；可通过 COURT_PYTHON 指定现有解释器。" >&2
  exit 2
fi
export PYTHONDONTWRITEBYTECODE=1
exec "$court_python" -B -u "$court_package_dir/launch.py" "$@"
