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
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, unquote

HERE = os.path.dirname(os.path.abspath(__file__))

BRIDGE_PORT = int(os.environ.get('BRIDGE_PORT', '18080'))
SEREIN_URL = os.environ.get('SEREIN_URL', 'http://127.0.0.1:18217').rstrip('/')
API_UPSTREAM = os.environ.get('API_UPSTREAM', 'https://daddy-six-vert.vercel.app').rstrip('/')
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

        # /serein/* 注入 Gateway Key；其它路径保留客户端自带 Authorization
        if path.startswith('/serein'):
            if KEY:
                headers['Authorization'] = 'Bearer ' + KEY

        conn = cls(host, port, timeout=180)
        try:
            conn.request(self.command, path, body=body, headers=headers)
            resp = conn.getresponse()
        except Exception as e:
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

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return self.rfile.read(n) if n else None

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/health':
            self._json(200, {'ok': True, 'serein': SEREIN_URL, 'key': bool(KEY), 'app': APP_DIR})
            return
        if path.startswith('/serein') or path.startswith('/api'):
            self._relay(SEREIN_URL if path.startswith('/serein') else API_UPSTREAM, self.path, None)
            return
        super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._body()
        if path.startswith('/serein'):
            self._relay(SEREIN_URL, self.path, body)
            return
        if path.startswith('/api'):
            self._relay(API_UPSTREAM, self.path, body)
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


def main():
    log('serein   =', SEREIN_URL)
    log('upstream =', API_UPSTREAM)
    log('app      =', APP_DIR)
    if KEY:
        log('key      = loaded (%d chars)' % len(KEY))
    else:
        log('key      = NOT FOUND — 请在 Termux 里确认 ~/Serein/deploy/secrets/api-token 存在')
    log('open     = http://127.0.0.1:%d/' % BRIDGE_PORT)
    srv = ThreadingHTTPServer(('0.0.0.0', BRIDGE_PORT), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log('bye')


if __name__ == '__main__':
    main()
