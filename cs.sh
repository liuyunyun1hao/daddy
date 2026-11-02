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

echo "== 4/5 写入并启动本地桥 =="
cd "$BR_DIR" || { echo "  !! $BR_DIR 进不去"; exit 1; }
cat > serein-bridge.py <<'BRIDGE_EOF'
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Serein 本地桥 —— 让手机浏览器里的 Caeruleus 能连上 Termux 里的 Serein。

为什么要这个桥（直连不行的三个原因）：
  1) Serein 的 MCP 端点只接受自己的 Origin（其它 Origin 一律 403 Invalid origin）
  2) Serein 不返回 CORS 头，跨域请求在浏览器里必定失败
  3) GitHub Pages 是 https，直连 http://127.0.0.1 会被 mixed-content 拦死

桥做的事：
  · 用 http://127.0.0.1:PORT 托管 index.html（页面与桥同源 → 没有跨域、没有 mixed content）
  · /serein/*  转发到 Serein 的 /serein/*，删掉 Origin/Referer，注入 Authorization: Bearer <Gateway Key>
  · /api/*     转发到 Vercel（聊天、额度用）
  · 除 /serein/* 外原样透传

用法（Termux）：
    cp /sdcard/Download/Daddy/* ~/caeruleus/
    cd ~/caeruleus && python serein-bridge.py
  然后手机 Chrome 打开 http://127.0.0.1:18080/

可选环境变量：
    BRIDGE_PORT=18080
    SEREIN_URL=http://127.0.0.1:18217
    SEREIN_KEY=<直接用这个 Key，不读文件>
    SEREIN_KEY_FILE=~/Serein/deploy/secrets/api-token
    API_UPSTREAM=https://daddy-six-vert.vercel.app
    APP_DIR=<静态文件目录，默认脚本所在目录>
"""
import os
import sys
import json
import socket
import http.client
import urllib.request
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, unquote

HERE = os.path.dirname(os.path.abspath(__file__))

BRIDGE_PORT = int(os.environ.get('BRIDGE_PORT', '18080'))
SEREIN_URL = os.environ.get('SEREIN_URL', 'http://127.0.0.1:18217').rstrip('/')
API_UPSTREAM = os.environ.get('API_UPSTREAM', 'http://127.0.0.1:9355/zen/go/v1').rstrip('/')
FALLBACK_UPSTREAM = 'https://opencode.ai/zen/go/v1'
BROWSER_UA = ('Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36')
APP_DIR = os.environ.get('APP_DIR', HERE)

KEY_CANDIDATES = [
    os.environ.get('SEREIN_KEY_FILE', ''),
    os.path.expanduser('~/Serein/deploy/secrets/api-token'),
    os.path.expanduser('~/serein/deploy/secrets/api-token'),
    os.path.join(HERE, 'serein-key.txt'),
]

HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
       'te', 'trailers', 'transfer-encoding', 'upgrade', 'host', 'content-length'}


def read_key():
    k = os.environ.get('SEREIN_KEY', '').strip()
    if k:
        return k
    for p in KEY_CANDIDATES:
        if p and os.path.isfile(p):
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    v = f.read().strip()
                if v:
                    return v
            except Exception:
                pass
    return ''


KEY = read_key()


def log(*a):
    print('[bridge]', *a, flush=True)


APP_UPSTREAM = os.environ.get('APP_UPSTREAM', 'https://liuyunyun1hao.github.io/daddy')
APP_FILES = ['index.html', 'manifest.webmanifest', 'sw.js', 'icon-192.png', 'icon-512.png']


def ensure_app(force=False):
    """页面文件不在本地时，从线上仓库拉一份下来（终端里不用再 cp 一堆文件）。"""
    got = 0
    for name in APP_FILES:
        dst = os.path.join(APP_DIR, name)
        if not force and os.path.isfile(dst) and os.path.getsize(dst) > 200:
            continue
        url = APP_UPSTREAM.rstrip('/') + '/' + name
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'CaeruleusBridge/1.0'})
            with urllib.request.urlopen(req, timeout=40) as r:
                data = r.read()
            if len(data) > 200:
                with open(dst, 'wb') as f:
                    f.write(data)
                got += 1
                log('fetched', name, len(data), 'bytes')
        except Exception as e:
            log('fetch failed', name, ':', e)
    if not got:
        log('页面文件齐了，跳过下载')
    return os.path.isfile(os.path.join(APP_DIR, 'index.html'))


class Handler(SimpleHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'CaeruleusBridge/1.0'

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=APP_DIR, **kw)

    # ---------- helpers ----------
    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers',
                         'Content-Type, Authorization, Accept, Mcp-Session-Id, Last-Event-ID, X-Serein-Window-ID')
        self.send_header('Access-Control-Expose-Headers', 'Mcp-Session-Id')
        self.send_header('Access-Control-Allow-Private-Network', 'true')

    def _relay(self, origin, path, body):
        u = urlparse(origin)
        host = u.hostname or '127.0.0.1'
        port = u.port or (443 if u.scheme == 'https' else 80)
        cls = http.client.HTTPSConnection if u.scheme == 'https' else http.client.HTTPConnection

        headers = {}
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in HOP or lk in ('origin', 'referer'):
                continue
            headers[k] = v
        headers['Host'] = host if port in (80, 443) else '%s:%d' % (host, port)
        if host == 'opencode.ai':
            headers['User-Agent'] = BROWSER_UA
            headers['Accept-Language'] = 'zh-CN,zh;q=0.9,en;q=0.8'
            headers.pop('Host', None)
            headers['Host'] = 'opencode.ai'

        # /serein/* 注入 Gateway Key；其它路径保留客户端自带 Authorization
        if path.startswith('/serein'):
            if not KEY:
                self._json(500, {'error': 'bridge has no gateway key',
                                 'hint': '把 Serein 的 Gateway Key 放进 ~/Serein/deploy/secrets/api-token，或用 SEREIN_KEY=... 启动桥'})
                return
            headers['Authorization'] = 'Bearer ' + KEY

        conn = cls(host, port, timeout=180)
        try:
            conn.request(self.command, path, body=body, headers=headers)
            resp = conn.getresponse()
        except Exception as e:
            log('upstream failed:', self.command, path, '->', type(e).__name__, e)
            self._json(502, {'error': 'upstream unreachable', 'detail': str(e)})
            try:
                conn.close()
            except Exception:
                pass
            return

        ctype = (resp.getheader('Content-Type') or '').lower()
        streaming = ctype.startswith('text/event-stream')

        if streaming:
            self.send_response(resp.status)
            self._cors()
            for k, v in resp.getheaders():
                if k.lower() in HOP:
                    continue
                self.send_header(k, v)
            self.send_header('Cache-Control', 'no-cache, no-transform')
            self.send_header('X-Accel-Buffering', 'no')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            try:
                while True:
                    chunk = resp.read1(4096) if hasattr(resp, 'read1') else resp.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(b'%X\r\n' % len(chunk) + chunk + b'\r\n')
                    self.wfile.flush()
                self.wfile.write(b'0\r\n\r\n')
                self.wfile.flush()
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            return

        data = resp.read()
        self.send_response(resp.status)
        self._cors()
        for k, v in resp.getheaders():
            if k.lower() in HOP:
                continue
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self._cors()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    # ---------- verbs ----------
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _api_path(self):
        """把 /api/proxy?p=/chat/completions 或 /api/models 还原成上游路径。"""
        from urllib.parse import parse_qs, urlencode
        p = urlparse(self.path)
        path = p.path[len('/api'):] if p.path.startswith('/api') else p.path
        q = parse_qs(p.query, keep_blank_values=True)
        if path in ('/proxy', '/proxy/'):
            path = q.pop('p', ['/models'])[0]
        rest = urlencode({k: v[0] for k, v in q.items()})
        return path + ('?' + rest if rest else '')

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return self.rfile.read(n) if n else None

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/health':
            self._json(200, {'ok': True, 'serein': SEREIN_URL, 'key': bool(KEY), 'app': APP_DIR})
            return
        if path.startswith('/serein'):
            self._relay(SEREIN_URL, self.path, None)
            return
        if path.startswith('/api'):
            self._relay(API_UPSTREAM, self._api_path(), None)
            return
        super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._body()
        if path.startswith('/serein'):
            self._relay(SEREIN_URL, self.path, body)
            return
        if path.startswith('/api'):
            self._relay(API_UPSTREAM, self._api_path(), body)
            return
        self._json(404, {'error': 'not found'})

    def do_DELETE(self):
        path = urlparse(self.path).path
        body = self._body()
        if path.startswith('/serein'):
            self._relay(SEREIN_URL, self.path, body)
            return
        self._json(404, {'error': 'not found'})

    def log_message(self, fmt, *args):
        log(self.address_string(), fmt % args)


def bind_any(start, tries=12):
    """端口被占就往后找，别让人卡在 Errno 98 上。"""
    for p in range(start, start + tries):
        try:
            return ThreadingHTTPServer(('0.0.0.0', p), Handler), p
        except OSError as e:
            if getattr(e, 'errno', None) not in (98, 48, 10048):
                raise
            log('port %d is busy, trying %d...' % (p, p + 1))
    raise SystemExit('端口 %d-%d 都被占用了，用 BRIDGE_PORT=19000 python serein-bridge.py 换一个' % (start, start + tries - 1))


def main():
    global API_UPSTREAM
    if '127.0.0.1:9355' in API_UPSTREAM:
        ok = False
        try:
            s_ = socket.create_connection(('127.0.0.1', 9355), timeout=1.5)
            s_.close(); ok = True
        except Exception:
            pass
        if not ok:
            log('!! oc 代理 9355 没起来 —— /api 会 502。先跑 cs 把 oc 拉起来再重开桥')
    log('serein   =', SEREIN_URL)
    log('upstream =', API_UPSTREAM)
    log('app      =', APP_DIR)
    if KEY:
        fp = (KEY[:4] + '…' + KEY[-4:]) if len(KEY) > 10 else '(short)'
        log('key      = loaded %d chars  %s' % (len(KEY), fp))
    else:
        log('key      = NOT FOUND — 请在 Termux 里确认 ~/Serein/deploy/secrets/api-token 存在')
    if not ensure_app():
        log('!! 没有 index.html，页面无法打开：检查网络，或手动把 index.html 放进', APP_DIR)
    srv, port = bind_any(BRIDGE_PORT)
    log('open     = http://127.0.0.1:%d/' % port)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log('bye')


if __name__ == '__main__':
    main()

BRIDGE_EOF
echo "  bridge written ($(wc -c < serein-bridge.py) bytes)"
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
