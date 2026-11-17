#!/usr/bin/env bash
# 一键启动常驻接活（macOS / Linux）
# 用法： ./start.sh              轮询间隔 5 秒，执行器读 agent.env 或默认 shell
#        ./start.sh 2            轮询间隔 2 秒（秒级响应）
#        ./start.sh 5 echo       指定执行器（echo 只回显，安全演示）
#        ./start.sh 5 shell --dry-run   预览模式，不真跑
set -e
cd "$(dirname "$0")"

INTERVAL="${1:-5}"
EXEC="${2:-}"
shift $(( $# >= 2 ? 2 : 1 )) 2>/dev/null || true
ARGS=(start --interval "$INTERVAL")
if [ -n "$EXEC" ]; then
  ARGS+=(--executor "$EXEC")
fi
# 透传剩余参数（如 --dry-run / --once），但避免把数字重复带入
for a in "$@"; do
  case "$a" in
    --dry-run|--once) ARGS+=("$a") ;;
  esac
done

echo "==> python3 agent.py ${ARGS[*]}"
exec python3 agent.py "${ARGS[@]}"
