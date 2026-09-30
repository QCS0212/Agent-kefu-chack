#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
for name in api monitor; do
  pid_file=".run/$name.pid"
  [ -f "$pid_file" ] || continue
  pid="$(cat "$pid_file")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid"
    echo "已停止 $name (PID $pid)"
  else
    echo "$name (PID $pid) 未在运行"
  fi
  rm -f "$pid_file"
done