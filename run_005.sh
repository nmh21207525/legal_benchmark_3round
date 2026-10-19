#!/usr/bin/env bash
set -euo pipefail
court_workspace="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
for court_arg in "$@"; do
  case "$court_arg" in
    --case-id|--case-id=*|--mode|--mode=*)
      echo "run_005.sh 固定只运行005并自动评分；请勿重复指定案件或模式。" >&2
      exit 2 ;;
  esac
done
exec bash "$court_workspace/start.sh" --mode formal --case-id CA-PILOT-005 "$@"
