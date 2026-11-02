#!/data/data/com.termux/files/usr/bin/bash
# Caeruleus 2.0 一键安装 / 升级
set -u
D="$HOME/caeruleus"
BASE="https://liuyunyun1hao.github.io/daddy"
MIRROR="https://cdn.jsdelivr.net/gh/liuyunyun1hao/daddy@main"
mkdir -p "$D/app"

echo "[1/4] 下载服务端与页面…"
get() {  # get <remote-name> <local-path>
  if curl -fsSL -m 90 "$BASE/$1" -o "$2" 2>/dev/null && [ -s "$2" ]; then return 0; fi
  echo "      primary failed, trying mirror…"
  curl -fsSL -m 90 "$MIRROR/$1" -o "$2" 2>/dev/null && [ -s "$2" ]
}
get caeruleus.py "$D/caeruleus.py" || { echo "!! 服务端下载失败（两个源都不通）"; exit 1; }
get index.html "$D/app/index.html" || { echo "!! 页面下载失败"; exit 1; }
echo "      服务端 $(wc -c < "$D/caeruleus.py") 字节，页面 $(wc -c < "$D/app/index.html") 字节"

echo "[2/4] 停止旧进程…"
pkill -f "serein-bridge.py" 2>/dev/null
pkill -f "caeruleus.py" 2>/dev/null
sleep 1

echo "[3/4] 启动…"
cd "$D" || exit 1
nohup python3 caeruleus.py >> "$D/server.log" 2>&1 &
sleep 2

echo "[4/4] 自检…"
H=$(curl -s -m 5 http://127.0.0.1:18080/api/health || true)
if echo "$H" | grep -q '"ok": *true'; then
  echo "OK 已启动：$H"
  echo "打开 http://127.0.0.1:18080"
else
  echo "!! 没起来，日志："
  tail -20 "$D/server.log"
  exit 1
fi

cat > "$D/start.sh" <<'EOS'
#!/data/data/com.termux/files/usr/bin/bash
cd "$HOME/caeruleus" || exit 1
pkill -f caeruleus.py 2>/dev/null; sleep 1
nohup python3 caeruleus.py >> server.log 2>&1 &
sleep 2
curl -s -m 5 http://127.0.0.1:18080/api/health; echo
EOS
chmod +x "$D/start.sh"
echo "以后重启：bash ~/caeruleus/start.sh"
