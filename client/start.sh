#!/bin/bash
# 平台启动入口（任务书 10.2）: ./start.sh <playerId> <host> <port>
# 本地调测包会多传第 4 个参数 playerName，一并透传。
set -e
cd "$(dirname "$0")"

if [ "$#" -lt 3 ]; then
  echo "Usage: $0 <playerId> <host> <port> [playerName]" >&2
  exit 1
fi

exec python3 main.py "$@"
