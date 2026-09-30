#!/usr/bin/env bash
# 一键启动：批量检测 API（127.0.0.1:8000）+ 监测平台（127.0.0.1:8010）
#   bash scripts/start_all.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ ! -x ".venv/bin/python" ]; then
  echo "[1/4] 未找到 .venv，正在创建…"
  python3 -m venv .venv
fi
PY=".venv/bin/python"

echo "[2/4] 检查依赖…"
if ! "$PY" -c "import fastapi, httpx, pydantic, uvicorn" 2>/dev/null; then
  "$PY" -m pip install -r requirements.txt
fi

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "[3/4] 已从 .env.example 生成 .env，请按需修改令牌与模型配置"
else
  echo "[3/4] 使用已有 .env"
fi

mkdir -p .run out-logs
echo "[4/4] 启动服务…"
nohup "$PY" -B -m uvicorn app.api:app --host 127.0.0.1 --port 8000 --workers 1 > out-logs/api.log 2>&1 &
echo $! > .run/api.pid
sleep 2
nohup "$PY" -B -m monitor --host 127.0.0.1 --port 8010 > out-logs/monitor.log 2>&1 &
echo $! > .run/monitor.pid

echo
echo "批量检测 API : http://127.0.0.1:8000/docs   (PID $(cat .run/api.pid))"
echo "监测平台     : http://127.0.0.1:8010/docs   (PID $(cat .run/monitor.pid))"
echo "日志         : out-logs/*.log"
echo "停止服务     : bash scripts/stop_all.sh"