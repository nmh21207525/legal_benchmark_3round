#!/usr/bin/env bash
# Sourced by entrypoints. Keys are read without echo and stay in process memory.
legal_require_api_keys() {
  if [[ -t 0 ]]; then
    if [[ -z "${PACKY_API_KEY:-}" ]]; then
      read -r -s -p 'Packy API key（输入不显示）: ' PACKY_API_KEY
      printf '\n'
      export PACKY_API_KEY
    fi
    if [[ -z "${ARK_API_KEY:-}" ]]; then
      read -r -s -p 'Ark API key（输入不显示）: ' ARK_API_KEY
      printf '\n'
      export ARK_API_KEY
    fi
  fi
  if [[ -z "${PACKY_API_KEY:-}" || -z "${ARK_API_KEY:-}" ]]; then
    printf '%s\n' '请先设置 PACKY_API_KEY 和 ARK_API_KEY；交互式终端运行会隐藏输入提示。不要把密钥写进项目或日志。' >&2
    return 2
  fi
  export PACKY_BASE_URL="${PACKY_BASE_URL:-https://cf.api.fan/v1}"
  export ARK_BASE_URL="${ARK_BASE_URL:-https://ark.cn-beijing.volces.com/api/v3}"
}
