#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
BACKEND_DIR="$SCRIPT_DIR/backend"
FRONTEND_DIR="$SCRIPT_DIR/frontend"
PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
ENV_FILE="$SCRIPT_DIR/.env"

# 本地密钥只从被 Git 忽略的环境文件加载，不传给前端构建。
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  if ! command -v uv >/dev/null 2>&1; then
    echo "错误：未找到 uv，无法创建项目专属虚拟环境。" >&2
    echo "请安装 uv：https://docs.astral.sh/uv/" >&2
    exit 1
  fi
  echo "首次启动，正在使用 uv 创建项目虚拟环境……"
  (cd "$PROJECT_ROOT" && uv venv .venv)
  echo "正在安装后端依赖……"
  (cd "$PROJECT_ROOT" && uv pip install --python "$PYTHON_BIN" -r backend/requirements.txt)
fi

if [[ ! -f "$BACKEND_DIR/requirements.txt" ]]; then
  echo "错误：未找到后端依赖文件：$BACKEND_DIR/requirements.txt" >&2
  exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "错误：未找到 npm，请先安装 Node.js。" >&2
  exit 1
fi

if [[ ! -d "$FRONTEND_DIR/node_modules" ]]; then
  echo "首次启动，正在安装前端依赖……"
  (cd "$FRONTEND_DIR" && npm install)
fi

backend_pid=""
frontend_pid=""

cleanup() {
  trap - INT TERM EXIT
  echo
  echo "正在停止前后端服务……"
  [[ -n "$frontend_pid" ]] && kill "$frontend_pid" 2>/dev/null || true
  [[ -n "$backend_pid" ]] && kill "$backend_pid" 2>/dev/null || true
  [[ -n "$frontend_pid" ]] && wait "$frontend_pid" 2>/dev/null || true
  [[ -n "$backend_pid" ]] && wait "$backend_pid" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo "正在启动 FastAPI：http://localhost:8000"
(cd "$BACKEND_DIR" && exec "$PYTHON_BIN" run.py) &
backend_pid=$!

echo "正在启动 Vue：http://localhost:5173"
(cd "$FRONTEND_DIR" && exec npm run dev -- --host 0.0.0.0) &
frontend_pid=$!

echo "服务已启动，按 Ctrl+C 同时停止前后端。"
echo "FastAPI 文档：http://localhost:8000/docs"

status=0
while kill -0 "$backend_pid" 2>/dev/null && kill -0 "$frontend_pid" 2>/dev/null; do
  sleep 1
done
set +e
if ! kill -0 "$backend_pid" 2>/dev/null; then
  wait "$backend_pid"
  status=$?
else
  wait "$frontend_pid"
  status=$?
fi
set -e
echo "检测到一个服务退出，正在关闭另一个服务……"
exit "$status"
