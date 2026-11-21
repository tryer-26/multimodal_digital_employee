#!/usr/bin/env bash
# MDE-LAB 多模态数字员工平台 —— 启动脚本（Linux / macOS）
#
# 用法：
#   ./scripts/start.sh            # 同时启动中台(8000) 与 前端(3000)
#   ./scripts/start.sh backend    # 只启动中台
#   ./scripts/start.sh frontend   # 只启动前端
#
# 说明：中台以 --reload 模式运行，且仅监听 core/ 与 mcp_servers/ 下的 *.py；
#       修改或新建 .env 不会触发热重载，需手动重启中台。
set -euo pipefail

TARGET="${1:-all}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

init_env() {
  if [[ ! -f "$ROOT/.env" ]]; then
    cp "$ROOT/.env.example" "$ROOT/.env"
    echo "[警告] 未找到 .env，已从 .env.example 复制；请填入 LLM_API_KEY / SILICONFLOW_API_KEY 后重启中台。"
  fi
}

python_exe() {
  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    echo "$ROOT/.venv/bin/python"
  else
    echo "[警告] 未找到 .venv 虚拟环境，回退使用系统 python。" >&2
    echo "python3"
  fi
}

start_backend() {
  init_env
  local py
  py="$(python_exe)"
  echo "[中台] http://localhost:8000"
  cd "$ROOT"
  # --reload-dir 收窄监听范围，避免 .venv/data/logs 内文件变更触发重载风暴
  exec "$py" -m uvicorn core.main:app --host 0.0.0.0 --port 8000 \
    --reload --reload-dir core --reload-dir mcp_servers
}

start_frontend() {
  cd "$ROOT/frontend"
  if [[ ! -d node_modules ]]; then
    echo "[前端] 首次运行，安装依赖..."
    npm install
  fi
  echo "[前端] http://localhost:3000"
  exec npm run dev
}

case "$TARGET" in
  backend)
    start_backend
    ;;
  frontend)
    start_frontend
    ;;
  all)
    start_backend &
    BACKEND_PID=$!
    sleep 3
    start_frontend &
    FRONTEND_PID=$!

    # 任一进程退出或收到中断信号时，连带停止另一个
    trap 'kill "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true' INT TERM EXIT
    echo "已启动中台(PID $BACKEND_PID) 与 前端(PID $FRONTEND_PID)。"
    echo "访问：http://localhost:3000/login （演示账号 user01 / agent01 / admin，密码 123456）"
    echo "停止：Ctrl+C"
    wait
    ;;
  *)
    echo "用法：$0 [all|backend|frontend]" >&2
    exit 1
    ;;
esac
