#!/data/data/com.termux/files/usr/bin/bash
# Caeruleus 一键启动：oc 代理(9355) + 本地桥(18080)
# 用法：cs        启动
#       cs stop   停止
set -u
OC_DIR="$HOME/opencode-go-proxy-for-trae"
BR_DIR="$HOME/caeruleus"
KEY_FILE="$HOME/Serein/deploy/secrets/api-token"

stop_all() {
  pkill -f opencode-go-proxy-for-trae.py 2>/dev/null
  pkill -f serein-bridge.py 2>/dev/null
  sleep 1
}

if [ "${1:-}" = "stop" ]; then
  echo "== 停止 oc + 桥 =="
  stop_all
  echo "已停止（Serein 请用 se 菜单停）"
  exit 0
fi

mkdir -p "$BR_DIR"
echo "== 1/5 清理旧进程 =="
stop_all

echo "== 2/5 启动 oc 代理 =="
if [ -f "$OC_DIR/opencode-go-proxy-for-trae.py" ]; then
  cd "$OC_DIR" || exit 1
  nohup python opencode-go-proxy-for-trae.py > "$HOME/oc.log" 2>&1 &
  sleep 2
  if grep -q "listening" "$HOME/oc.log" 2>/dev/null; then
    grep -m1 "listening" "$HOME/oc.log"
  else
    echo "!! oc 没起来，最后几行："
    tail -3 "$HOME/oc.log" 2>/dev/null
  fi
else
  echo "!! 没找到 $OC_DIR（先 git clone 那个仓库）"
fi

echo "== 3/5 检查 Serein =="
SC=$(curl -s -o /dev/null -w "%{http_code}" -m 5 -X POST http://127.0.0.1:18217/serein/mcp \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' 2>/dev/null)
if [ "$SC" = "000" ]; then
  echo "  Serein 没起。它缺依赖（PyYAML 等），用 se 菜单启动最稳："
  echo "    se  →  4  →  启动"
  echo "  （若想修依赖： pip install pyyaml jieba fastapi uvicorn mcp）"
  SC="000"
else
  echo "  Serein 在跑（$SC）"
fi

echo "== 4/5 启动本地桥 =="
NEW_OK=0
for SRC in "$HOME/storage/downloads/Daddy/serein-bridge.py" "/sdcard/Download/Daddy/serein-bridge.py"; do
  if [ -f "$SRC" ] && grep -q "127.0.0.1:9355" "$SRC" 2>/dev/null; then
    cp "$SRC" serein-bridge.py
    NEW_OK=1
    echo "  bridge ← $SRC"
    break
  fi
done
if [ "$NEW_OK" = "0" ]; then
  i=0
  while [ $i -lt 3 ]; do
    i=$((i+1))
    if curl -fsSL -m 20 -o serein-bridge.py.new "https://liuyunyun1hao.github.io/daddy/serein-bridge.py?t=$(date +%s)-$i" \
       && grep -q "127.0.0.1:9355" serein-bridge.py.new 2>/dev/null; then
      mv serein-bridge.py.new serein-bridge.py
      NEW_OK=1
      echo "  bridge ← pages"
      break
    fi
    sleep 2
  done
  rm -f serein-bridge.py.new
fi
if [ "$NEW_OK" = "0" ]; then
  echo "  !! 拿不到新版桥，继续用现有的（可能仍走 vercel，/api 会卡）"
fi
KEY=""
[ -f "$KEY_FILE" ] && KEY=$(cat "$KEY_FILE" 2>/dev/null)
SEREIN_KEY="$KEY" nohup python serein-bridge.py > bridge.log 2>&1 &
sleep 3
sed -n '1,6p' bridge.log 2>/dev/null

echo "== 5/5 访问地址 =="
IP=$(ifconfig 2>/dev/null | grep -oE 'inet (addr:)?[0-9.]+' | awk '{print $2}' | sed 's/addr://' | grep -v '^127\.' | head -1)
echo "  本机      http://127.0.0.1:18080/"
[ -n "${IP:-}" ] && echo "  局域网    http://$IP:18080/"
[ -z "${IP:-}" ] && echo "  （没取到局域网 IP；开热点后一般是 192.168.43.1）"

echo "  Serein    $SC  $([ "$SC" = "401" ] && echo '（在跑，正常）' || echo '（没起，看 ~/serein.log）')"
echo
echo "停止：cs stop"
