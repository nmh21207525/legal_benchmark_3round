#!/usr/bin/env bash
# Shared launcher environment. This file is sourced, never a model request.
court_workspace="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONDONTWRITEBYTECODE=1
if [[ -d "$court_workspace/.tools/node_modules/.bin" ]]; then
  export PATH="$court_workspace/.tools/node_modules/.bin:$PATH"
fi

court_select_python() {
  if [[ -n "${COURT_PYTHON:-}" ]]; then
    court_python="$(command -v -- "$COURT_PYTHON" || true)"
    if [[ -z "$court_python" || ! -x "$court_python" ]]; then
      echo "COURT_PYTHON 指向的解释器不可执行：$COURT_PYTHON" >&2
      return 2
    fi
  elif [[ -x "$court_workspace/.venv/bin/python" ]]; then
    court_python="$court_workspace/.venv/bin/python"
  elif [[ -x /home/nmh/legal_agent/.venv/bin/python ]]; then
    court_python=/home/nmh/legal_agent/.venv/bin/python
  else
    court_python="$(command -v python3 || true)"
  fi
  if [[ -z "$court_python" || ! -x "$court_python" ]]; then
    echo "需要 Python 3.10+。可用 COURT_PYTHON 指定已有 Python，再运行 bash setup.sh。" >&2
    return 2
  fi
  "$court_python" -B -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else "需要 Python 3.10+；请用 COURT_PYTHON 指定解释器。")'
}
