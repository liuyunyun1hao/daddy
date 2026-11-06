#!/data/data/com.termux/files/usr/bin/bash
# Latent 记忆服务 · Termux 启动脚本
# 用法：bash start-latent.sh            （只允许本机）
#       bash start-latent.sh --lan      （允许局域网，供另一台手机/电脑连）
#       bash start-latent.sh --lan --token=xxx   （局域网 + 自定义口令）
# token 不填会自动生成（数据目录/token），启动日志里会打印配对链接。
set -e
cd "$(dirname "$0")"

HOST=127.0.0.1
PORT=8765
DATA="$HOME/latent-memory"
TOKEN="${LATENT_TOKEN:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --lan) HOST=0.0.0.0 ;;
    --port=*) PORT="${1#--port=}" ;;
    --port) shift; PORT="${1:-$PORT}" ;;
    --data=*) DATA="${1#--data=}" ;;
    --data) shift; DATA="${1:-$DATA}" ;;
    --token=*) TOKEN="${1#--token=}" ;;
    --token) shift; TOKEN="${1:-$TOKEN}" ;;
    *) echo "忽略未知参数：$1" ;;
  esac
  shift
done

# token 由服务端自动生成并强制校验；这里只提示一下局域网暴露面
if [ "$HOST" = "0.0.0.0" ]; then
  echo "提示：已开 --lan，同网段设备需要 token 才能访问（启动日志会打印配对链接）。"
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "没有 python3。先在 Termux 里执行：pkg install python"
  exit 1
fi

echo "服务地址：http://$HOST:$PORT"
echo "Daddy 页面：http://127.0.0.1:$PORT/"
echo "数据目录：$DATA"
echo "按 Ctrl+C 停止。"
ARGS=(--host "$HOST" --port "$PORT" --data "$DATA" --static "$(pwd)" \
  --proxy "${DADDY_API:-http://127.0.0.1:9355/zen/go/v1}")
if [ -n "$TOKEN" ]; then ARGS+=(--token "$TOKEN"); fi
exec python3 latent-server.py "${ARGS[@]}"
