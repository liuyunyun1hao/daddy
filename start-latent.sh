#!/data/data/com.termux/files/usr/bin/bash
# Latent 记忆服务 · Termux 启动脚本
# 用法：bash start-latent.sh            （只允许本机）
#       bash start-latent.sh --lan      （允许局域网，供另一台手机/电脑连）
set -e
cd "$(dirname "$0")"

HOST=127.0.0.1
PORT=8765
DATA="$HOME/latent-memory"

for a in "$@"; do
  case "$a" in
    --lan) HOST=0.0.0.0 ;;
    --port=*) PORT="${a#--port=}" ;;
    --data=*) DATA="${a#--data=}" ;;
  esac
done

if ! command -v python3 >/dev/null 2>&1; then
  echo "没有 python3。先在 Termux 里执行：pkg install python"
  exit 1
fi

echo "服务地址：http://$HOST:$PORT"
echo "Daddy 页面：http://127.0.0.1:$PORT/"
echo "数据目录：$DATA"
echo "按 Ctrl+C 停止。"
exec python3 latent-server.py --host "$HOST" --port "$PORT" --data "$DATA" \
  --static "$(pwd)" \
  --proxy "${DADDY_API:-https://daddy-six-vert.vercel.app/api}"
