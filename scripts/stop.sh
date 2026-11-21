#!/usr/bin/env bash
# MDE-LAB 多模态数字员工平台 —— 停止脚本（Linux / macOS）
#
# 用法：
#   ./scripts/stop.sh              # 停止中台(8000) 与 前端(3000)
#   ./scripts/stop.sh 8000         # 只停止指定端口
set -uo pipefail

PORTS=("$@")
if [[ ${#PORTS[@]} -eq 0 ]]; then
  PORTS=(8000 3000)
fi

stop_port() {
  local port="$1"
  local pids=""

  if command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -ti "tcp:$port" -sTCP:LISTEN 2>/dev/null || true)"
  elif command -v fuser >/dev/null 2>&1; then
    pids="$(fuser "$port/tcp" 2>/dev/null || true)"
  else
    echo "[错误] 未找到 lsof 或 fuser，无法定位端口 $port 的进程。" >&2
    return 1
  fi

  if [[ -z "$pids" ]]; then
    echo "端口 $port 无监听进程。"
    return 0
  fi

  # shellcheck disable=SC2086
  kill $pids 2>/dev/null || true
  sleep 1
  # shellcheck disable=SC2086
  kill -9 $pids 2>/dev/null || true
  echo "已停止端口 $port（PID: $pids）。"
}

for port in "${PORTS[@]}"; do
  stop_port "$port"
done
