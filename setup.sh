#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common_env.sh"
court_check_only=0
court_candidate=all
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check-only) court_check_only=1; shift ;;
    --candidate)
      [[ $# -gt 1 ]] || { echo "--candidate 后需要 all/seed/deepseek/opus/gpt" >&2; exit 2; }
      court_candidate="$2"; shift 2 ;;
    --help|-h)
      echo "用法：bash setup.sh [--check-only] [--candidate all|seed|deepseek|opus|gpt]"
      echo "默认创建工作区 .venv 并安装 requirements.txt；--check-only 仅检查，不联网安装。"
      echo "随后只检查 CLI 版本，不请求模型，不读取或保存密钥。"
      exit 0 ;;
    *) echo "不认识的参数：$1" >&2; exit 2 ;;
  esac
done
case "$court_candidate" in all|seed|deepseek|opus|gpt) ;; *) echo "不支持的 candidate：$court_candidate" >&2; exit 2 ;; esac
court_select_python
if [[ "$court_check_only" == 0 ]]; then
  if [[ ! -x "$court_workspace/.venv/bin/python" ]]; then
    echo "[环境] 创建 $court_workspace/.venv"
    if ! "$court_python" -B -m venv "$court_workspace/.venv"; then
      echo "当前 Python 无法创建 venv。请使用已安装 venv/ensurepip 的 Python 3.10+，例如 COURT_PYTHON=/完整路径/python bash setup.sh。" >&2
      exit 2
    fi
  fi
  court_python="$court_workspace/.venv/bin/python"
  "$court_python" -B -m pip install --disable-pip-version-check -r "$court_workspace/requirements.txt"
fi
"$court_python" -B - "$court_workspace" "$court_candidate" <<'PY'
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

root = Path(sys.argv[1])
candidate = sys.argv[2]
issues = []
print('[环境] Python ' + sys.version.split()[0] + '：' + sys.executable)
for name, expected in [('httpx', '0.28.1'), ('pypdf', '6.10.0')]:
    try:
        actual = version(name)
    except PackageNotFoundError:
        actual = None
    if actual != expected:
        issues.append(f'{name} 需要 {expected}，当前 {actual or "未安装"}；运行 bash setup.sh。')
    else:
        print(f'[依赖] {name} {actual}')

config_path = root / 'config.local.json'
if not config_path.is_file():
    config_path = root / 'config.example.json'
try:
    config = json.loads(config_path.read_text(encoding='utf-8'))
except (OSError, ValueError):
    raise SystemExit('[缺项] 配置无法读取：' + str(config_path))
codex = str(config.get('codex', {}).get('executable', 'codex'))
if not Path(codex).is_absolute() and ('/' in codex or '\\' in codex):
    codex = str((root / codex).resolve())
# Matches the runner's default; --claude-executable may explicitly override it.
legacy_claude = Path('/home/nmh/.local/claude-court/node_modules/.bin/claude')
claude = shutil.which('claude') or (str(legacy_claude) if legacy_claude.is_file() else 'claude')
needed = [('Codex', codex, (0, 160, 0), False)]
if candidate in ('all', 'opus'):
    needed.append(('Claude', claude, (2, 1, 300), True))
for label, executable, required, exact in needed:
    binary = shutil.which(executable)
    if not binary:
        issues.append(f'{label} CLI 未找到：{executable}')
        continue
    try:
        result = subprocess.run([binary, '--version'], capture_output=True,
                                text=True, timeout=15, check=False)
        match = re.search(r'\b(\d+)\.(\d+)\.(\d+)\b', result.stdout)
        got = tuple(map(int, match.groups())) if match else None
        valid = result.returncode == 0 and got is not None and (got == required if exact else got >= required)
    except (OSError, subprocess.TimeoutExpired):
        valid, got = False, None
    required_text = '.'.join(map(str, required))
    if not valid:
        issues.append(f'{label} CLI 需要 {"恰为 " if exact else "至少 "}{required_text}；当前 {".".join(map(str,got)) if got else "无法读取版本"}。')
    else:
        print(f'[CLI] {label} {".".join(map(str,got))}：{binary}')
if issues:
    for issue in issues:
        print('[缺项] ' + issue, file=sys.stderr)
    if any('CLI' in issue for issue in issues):
        print('已有 npm 时，可在工作区安装实验使用的 CLI（不需要 sudo）：', file=sys.stderr)
        print('npm install --prefix .tools @openai/codex@0.160.0 @anthropic-ai/claude-code@2.1.300', file=sys.stderr)
        print('安装后重新执行 bash setup.sh --check-only；若版本包不可用，请提供对应 CLI 路径，不要替换模型或放宽版本检查。', file=sys.stderr)
    raise SystemExit(2)
print('[就绪] 本地依赖与 CLI 版本检查通过；正式启动仍会进行接口预检。')
print('[下一步] 同事：bash run_005.sh --background；自己：bash run_006.sh --background。')
PY
