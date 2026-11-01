#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Caeruleus · 本地桥（Termux）
- 把 http://127.0.0.1:18001/mcp (OB) 和 18110/mcp (心潮) 包成带 CORS 的 /ob /xc
- 顺手把 /api/* 转发到线上代理，这样整页可以只跑在本机
- 只监听 127.0.0.1，不对外

用法：
  python3 mcp-bridge.py            # 默认 18080
  BRIDGE_PORT=18080 python3 mcp-bridge.py
"""
import os, sys, json, base64, http.server, socketserver, urllib.request, urllib.error, mimetypes, ssl
from urllib.parse import urlparse, urlunparse

PORT = int(os.environ.get("BRIDGE_PORT", "18080"))
OB_URL = os.environ.get("OB_URL", "http://127.0.0.1:18001")
XC_URL = os.environ.get("XC_URL", "http://127.0.0.1:18110")
OB_TOKEN = os.environ.get("OB_TOKEN", "GtjkUB3Qupx0hXcSySIvVDnrA2muDLoBB7k8PCaK-cA")
XC_TOKEN = os.environ.get("XC_TOKEN", "d8b566f10cc1bf4fbbaa1b506f548d5c120f62b433db3bb15d2d60ffb7162cd2")
API_UPSTREAM = os.environ.get("API_UPSTREAM", "https://daddy-six-vert.vercel.app")
APP_DIR = os.environ.get("APP_DIR", os.path.dirname(os.path.abspath(__file__)))

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "authorization, content-type, accept, mcp-session-id, mcp-protocol-version",
    "Access-Control-Expose-Headers": "mcp-session-id",
    "Access-Control-Allow-Private-Network": "true",
    "Access-Control-Max-Age": "86400",
}


def upstream_for(path):
    if path == "/ob" or path.startswith("/ob/"):
        return OB_URL.rstrip("/") + "/mcp" + path[3:], OB_TOKEN
    if path == "/xc" or path.startswith("/xc/"):
        return XC_URL.rstrip("/") + "/mcp" + path[3:], XC_TOKEN
    return None, None


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CaeruleusBridge/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _cors(self):
        for k, v in CORS.items():
            self.send_header(k, v)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------- GET：静态页 / 健康检查 ----------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            body = json.dumps({"ok": True, "ob": OB_URL, "xc": XC_URL, "app": APP_DIR}).encode()
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/api/"):
            return self._proxy_api("GET", b"")
        return self._static(path)

    def _static(self, path):
        rel = path.lstrip("/") or "index.html"
        if "/" in rel and not os.path.isfile(os.path.join(APP_DIR, rel)):
            rel = "index.html"
        full = os.path.abspath(os.path.join(APP_DIR, rel))
        if not full.startswith(os.path.abspath(APP_DIR)) or not os.path.isfile(full):
            full = os.path.join(APP_DIR, "index.html")
        if not os.path.isfile(full):
            self.send_error(404, "index.html not found next to mcp-bridge.py")
            return
        data = open(full, "rb").read()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if full.endswith(".webmanifest"):
            ctype = "application/manifest+json"
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # ---------- POST：MCP / API ----------
    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if path.startswith("/api/"):
            return self._proxy_api("POST", body)
        target, token = upstream_for(path)
        if not target:
            return self.send_error(404, "unknown path")
        return self._proxy_mcp(target, token, body)

    # ---------- 转发 MCP ----------
    def _proxy_mcp(self, url, token, body):
        headers = {
            "Content-Type": self.headers.get("Content-Type") or "application/json",
            "Accept": self.headers.get("Accept") or "application/json, text/event-stream",
            "Authorization": "Bearer " + token,
        }
        sid = self.headers.get("mcp-session-id")
        if sid:
            headers["Mcp-Session-Id"] = sid
        ver = self.headers.get("mcp-protocol-version")
        if ver:
            headers["MCP-Protocol-Version"] = ver
        req = urllib.request.Request(url, data=body if body else None, headers=headers,
                                     method=self.command)
        try:
            resp = urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self._cors()
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            for k in ("mcp-session-id",):
                if e.headers.get(k):
                    self.send_header("Mcp-Session-Id", e.headers.get(k))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        except Exception as e:
            self.send_response(502)
            self._cors()
            msg = json.dumps({"error": "upstream unreachable: %s" % e}).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return

        ctype = resp.headers.get("Content-Type", "application/json")
        sid_out = resp.headers.get("Mcp-Session-Id")
        streaming = "text/event-stream" in ctype
        self.send_response(resp.status)
        self._cors()
        self.send_header("Content-Type", ctype)
        if sid_out:
            self.send_header("Mcp-Session-Id", sid_out)
        if streaming:
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                while True:
                    chunk = resp.read(1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except Exception:
                pass
            self.close_connection = True
        else:
            data = resp.read()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    # ---------- 转发线上 API ----------
    def _proxy_api(self, method, body):
        url = API_UPSTREAM + self.path
        headers = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) Chrome/131.0 Safari/537.36"}
        for k in ("authorization", "content-type", "accept", "accept-language", "x-opencode-session"):
            v = self.headers.get(k)
            if v:
                headers[k] = v
        req = urllib.request.Request(url, data=body if body else None, headers=headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=180)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self._cors()
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        except Exception as e:
            self.send_response(502)
            self._cors()
            msg = json.dumps({"error": {"message": "api upstream failed: %s" % e}}).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return
        ctype = resp.headers.get("Content-Type", "application/json")
        streaming = "text/event-stream" in ctype
        self.send_response(resp.status)
        self._cors()
        self.send_header("Content-Type", ctype)
        if streaming:
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                while True:
                    chunk = resp.read(1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except Exception:
                pass
            self.close_connection = True
        else:
            data = resp.read()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    print("Caeruleus bridge on http://127.0.0.1:%d" % PORT)
    print("  OB -> %s   XC -> %s" % (OB_URL, XC_URL))
    print("  app dir: %s" % APP_DIR)
    Server(("127.0.0.1", PORT), Handler).serve_forever()
