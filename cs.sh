#!/data/data/com.termux/files/usr/bin/bash
# Caeruleus 一键启动：oc 代理(9355) + 页面 + 记忆服务(8765)
# 用法：cs        启动
#       cs stop   停止
set -u
OC_DIR="$HOME/opencode-go-proxy-for-trae"
# 脚本常被软链到 $PREFIX/bin（直接敲 cs），dirname "$0" 会指到 bin，这里先解开软链
SELF="$(readlink -f "$0" 2>/dev/null || echo "$0")"
BR_DIR="${DADDY_DIR:-$(cd "$(dirname "$SELF")" && pwd)}"

stop_all() {
  pkill -f opencode-go-proxy-for-trae.py 2>/dev/null
  pkill -f latent-server.py 2>/dev/null
  pkill -f serein-bridge.py 2>/dev/null
  sleep 1
}

if [ "${1:-}" = "stop" ]; then
  echo "== 停止 =="
  stop_all
  echo "已停止"
  exit 0
fi

mkdir -p "$BR_DIR"
cd "$BR_DIR" || { echo "!! $BR_DIR 进不去"; exit 1; }
echo "== 1/2 清理旧进程 =="
stop_all

echo "== 2/2 启动 oc 代理 =="
if [ -f "$OC_DIR/opencode-go-proxy-for-trae.py" ]; then
  ( cd "$OC_DIR" && nohup python opencode-go-proxy-for-trae.py > "$HOME/oc.log" 2>&1 & )
  sleep 2
  if grep -qmi "listening" "$HOME/oc.log" 2>/dev/null; then
    grep -mi "listening" "$HOME/oc.log" | head -1
  else
    echo "!! oc 没起来，最后几行："
    tail -3 "$HOME/oc.log" 2>/dev/null
  fi
else
  echo "!! 没找到 $OC_DIR（先 git clone 那个仓库）"
fi

echo "== 启动页面 + 记忆服务 =="
nohup bash start-latent.sh > "$HOME/latent.log" 2>&1 &
sleep 2
if curl -s -m 3 http://127.0.0.1:8765/health >/dev/null 2>&1; then
  echo "  页面      http://127.0.0.1:8765/"
  echo "  记忆服务  http://127.0.0.1:8765/mcp"
  LINK="$(grep -m1 -o 'http://[^ ]*/?t=[A-Za-z0-9_-]*' "$HOME/latent.log" 2>/dev/null || true)"
  if [ -n "$LINK" ]; then
    echo "  配对链接  $LINK"
  else
    echo "  （没找到配对链接，看 $HOME/latent.log）"
  fi
else
  echo "!! 没起来，最后几行："
  tail -5 "$HOME/latent.log" 2>/dev/null
fi

echo
echo "停止：cs stop"
