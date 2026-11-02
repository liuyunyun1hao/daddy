#!/data/data/com.termux/files/usr/bin/sh
# Caeruleus bridge one-shot fixer
DIR="$HOME/caeruleus"
mkdir -p "$DIR" || exit 1
cd "$DIR" || exit 1

echo "== 1/5 停掉旧桥 =="
pkill -f serein-bridge.py 2>/dev/null
sleep 1
if pgrep -f serein-bridge.py >/dev/null 2>&1; then
  echo "旧桥还在，强杀"
  pkill -9 -f serein-bridge.py 2>/dev/null
  sleep 1
fi

echo "== 2/5 找 Serein 的 Gateway Key =="
KEY="${SEREIN_KEY:-}"
if [ -z "$KEY" ] && [ -f "$HOME/Serein/deploy/secrets/api-token" ]; then
  KEY=$(cat "$HOME/Serein/deploy/secrets/api-token" 2>/dev/null)
fi
if [ -z "$KEY" ]; then
  echo "!! 没找到 Key（$HOME/Serein/deploy/secrets/api-token）"
else
  echo "key 长度 ${#KEY}"
fi
export SEREIN_KEY="$KEY"

echo "== 3/5 下载最新桥 =="
if command -v curl >/dev/null 2>&1; then
  curl -fsSL -o serein-bridge.py "https://liuyunyun1hao.github.io/daddy/serein-bridge.py" || echo "下载失败，用现有的"
else
  python -c "import urllib.request;urllib.request.urlretrieve('https://liuyunyun1hao.github.io/daddy/serein-bridge.py','serein-bridge.py')" || echo "下载失败，用现有的"
fi
ls -l serein-bridge.py 2>/dev/null

echo "== 4/5 后台启动 =="
nohup python serein-bridge.py > bridge.log 2>&1 &
sleep 3
echo "--- bridge.log ---"
head -8 bridge.log 2>/dev/null

echo "== 5/5 自检 =="
PORT=$(sed -n 's/.*open *= *http:\/\/127\.0\.0\.1:\([0-9]*\).*/\1/p' bridge.log 2>/dev/null | head -1)
PORT=${PORT:-18080}
echo "port = $PORT"
curl -s -o /dev/null -w "bridge = %{http_code}\n" -X POST "http://127.0.0.1:$PORT/serein/mcp" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
echo
echo "完成。若 bridge = 200，回浏览器刷新页面；若 401/500，把上面 bridge.log 前 8 行发我。"
