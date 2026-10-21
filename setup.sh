#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3,11), "需要 Python 3.11 或更新版本"'
"$PYTHON_BIN" -m venv --without-pip .venv
.venv/bin/python -m compileall -q shared_engine tools
.venv/bin/python -m unittest discover -s shared_engine -t . -p 'test*.py' -q
printf '%s\n' '环境及全部离线验收完成；无第三方Python依赖。运行 bash run_006.sh 或 bash run_005.sh，按提示隐藏输入API密钥。'
