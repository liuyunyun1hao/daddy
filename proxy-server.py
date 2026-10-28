#!/usr/bin/env python3
"""
Daddy UI —— 静态站点 + OpenCode Go 代理（零依赖，Python 3.8+ / curl）

为什么需要它：
  浏览器直连 https://opencode.ai/zen/go/v1/chat/completions 会被 CORS 拦下
  （该网关对 OPTIONS 预检返回 404，只有 /v1/models 能过），所以聊天和额度
  查询要经过同源转发。

为什么用 curl 而不是 urllib：
  Cloudflare 按 TLS 指纹 + UA 组合判定，Python 的 TLS ClientHello 会被
  Error 1010 browser_signature_banned 拦掉；curl 正常。

用法：
  python3 server.py [端口]        # 默认 8787

安全：
  - 不做任何存储，Key 只从请求头里取，转发完即丢弃。
  - 只允许转发到 opencode.ai，避免被当成任意主机的中转。
"""
import http.server
import json
import socketserver
import subprocess
import sys
import urllib.parse

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
ROOT = __file__.rsplit("/", 1)[0]
ALLOWED_HOST = "opencode.ai"
UPSTREAM = "https://opencode.ai/zen/go/v1"
CHUNK = 4096
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


class Handler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    # ---------- helpers ----------
    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization, X-Api-Key, X-Opencode-Session")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---------- routing ----------
    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path == "/api/__ver":
            return self.fail(200, "curl-v2")
        if self.path.startswith("/api/"):
            return self.proxy("GET")
        return super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/"):
            return self.proxy("POST")
        self.send_error(405)

    # ---------- proxy ----------
    def fail(self, code, message):
        msg = json.dumps({"error": {"message": message}}, ensure_ascii=False).encode()
        self.send_response(code)
        self.cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(msg)))
        self.end_headers()
        self.wfile.write(msg)

    def proxy(self, method):
        path = self.path.split("?")[0]
        target = UPSTREAM + path[len("/api"):]
        if "?" in self.path:
            target += "?" + self.path.split("?", 1)[1]
        if urllib.parse.urlparse(target).hostname != ALLOWED_HOST:
            return self.fail(403, "host not allowed")

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        args = ["curl", "-sS", "-N", "-X", method, target, "-D", "-",
                "-A", BROWSER_UA,
                "-H", "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8"]
        for h in ("Authorization", "X-Opencode-Session"):
            v = self.headers.get(h)
            if v:
                args += ["-H", "%s: %s" % (h, v)]
        if body:
            args += ["-H", "Content-Type: " + (self.headers.get("Content-Type") or "application/json")]
            args += ["--data-binary", "@-"]
        else:
            args += ["-H", "Accept: " + (self.headers.get("Accept") or "application/json")]

        try:
            p = subprocess.Popen(args,
                                 stdin=subprocess.PIPE if body else subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception as e:  # noqa: BLE001
            return self.fail(502, "curl 启动失败: %s" % e)
        if body:
            try:
                p.stdin.write(body)
                p.stdin.close()
            except BrokenPipeError:
                pass

        # 先把上游响应头读出来（-D - 会写在 body 前面）
        head = b""
        while b"\r\n\r\n" not in head and len(head) < 65536:
            ch = p.stdout.read(1)
            if not ch:
                break
            head += ch
        if not head:
            err = p.stderr.read().decode("utf-8", "replace")[:300]
            p.wait()
            return self.fail(502, "上游无响应 %s" % err)

        lines = head.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
        try:
            status = int(lines[0].split(" ")[1])
        except Exception:  # noqa: BLE001
            status = 502
        hdrs = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                hdrs[k.strip().lower()] = v.strip()

        ctype = hdrs.get("content-type", "application/json")

        # 统一用 close 定界，SSE 才能边收边转
        self.close_connection = True
        self.send_response(status)
        self.cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()

        try:
            while True:
                chunk = p.stdout.read(CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                p.stdout.close()
            except Exception:  # noqa: BLE001
                pass
            p.wait()


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    with Server(("0.0.0.0", PORT), Handler) as httpd:
        print("Daddy UI  ->  http://0.0.0.0:%d  (proxy: /api/* -> %s)" % (PORT, UPSTREAM))
        httpd.serve_forever()
