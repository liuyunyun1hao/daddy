#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Caeruleus server — zero dependency, SQLite backed.

- serves the single page app from <root>/app
- /api/snapshot  : everything at once (chat, brain, stage, diary, presets, cfg)
- /api/write     : {name, data, ts} -> upsert, last writer by ts wins
- /api/delete    : {name}
- /store/<name>  : legacy compatible GET/PUT/DELETE
- /api/oc/*      : proxy to the OpenCode Go local proxy
- /serein/*      : proxy to Serein MCP, injecting the gateway key
- /api/health    : for the tray / boot script
"""
import json, os, re, sqlite3, sys, threading, time, base64
from http.client import HTTPConnection, HTTPSConnection
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, unquote
from socketserver import ThreadingMixIn

HOME = os.path.expanduser("~")
ROOT = os.environ.get("CAERULEUS_HOME") or os.path.join(HOME, "caeruleus")
APP = os.path.join(ROOT, "app")
DB_PATH = os.path.join(ROOT, "data.db")
PORT = int(os.environ.get("CAERULEUS_PORT") or "18080")
OC_ORIGIN = os.environ.get("OC_ORIGIN") or "http://127.0.0.1:9355"
SEREIN_ORIGIN = os.environ.get("SEREIN_ORIGIN") or "http://127.0.0.1:18217"
KEY_FILE = os.environ.get("SEREIN_KEY_FILE") or os.path.join(HOME, "Serein", "deploy", "secrets", "api-token")
VERSION = "2.0.0"

os.makedirs(APP, exist_ok=True)

# ---------------------------------------------------------------- storage
_lock = threading.Lock()
_db = None

def db():
    global _db
    if _db is None:
        _db = sqlite3.connect(DB_PATH, check_same_thread=False)
        _db.execute("PRAGMA journal_mode=WAL")
        _db.execute("CREATE TABLE IF NOT EXISTS kv (name TEXT PRIMARY KEY, data TEXT NOT NULL, ts INTEGER NOT NULL, rev INTEGER NOT NULL DEFAULT 1)")
        _db.commit()
    return _db

def kv_all():
    rows = db().execute("SELECT name, data, ts, rev FROM kv").fetchall()
    out = {}
    for name, data, ts, rev in rows:
        try:
            out[name] = {"data": json.loads(data), "ts": ts, "rev": rev}
        except Exception:
            pass
    return out

def kv_get(name):
    row = db().execute("SELECT data, ts, rev FROM kv WHERE name=?", (name,)).fetchone()
    if not row:
        return None
    try:
        return {"data": json.loads(row[0]), "ts": row[1], "rev": row[2]}
    except Exception:
        return None

def kv_put(name, data, ts):
    with _lock:
        row = db().execute("SELECT ts, rev FROM kv WHERE name=?", (name,)).fetchone()
        if row and row[0] > ts:
            return False, row[1]
        rev = (row[1] + 1) if row else 1
        db().execute(
            "INSERT INTO kv(name,data,ts,rev) VALUES(?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET data=excluded.data, ts=excluded.ts, rev=excluded.rev",
            (name, json.dumps(data, ensure_ascii=False), ts, rev))
        db().commit()
        return True, rev

def kv_del(name):
    with _lock:
        db().execute("DELETE FROM kv WHERE name=?", (name,))
        db().commit()

# ---------------------------------------------------------------- key
def read_key():
    try:
        with open(KEY_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""

# ---------------------------------------------------------------- proxy
def _split(origin):
    u = urlparse(origin)
    return u.scheme, u.netloc, u.path.rstrip("/")

def relay(origin, path, method, body, headers=None):
    scheme, netloc, prefix = _split(origin)
    target = (prefix + path) if prefix else path
    cls = HTTPSConnection if scheme == "https" else HTTPConnection
    conn = cls(netloc, timeout=120)
    hdrs = {"accept": "application/json", "user-agent": "caeruleus/2.0"}
    if headers:
        hdrs.update(headers)
    try:
        conn.request(method, target, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, dict(resp.getheaders()), data
    finally:
        try:
            conn.close()
        except Exception:
            pass

# ---------------------------------------------------------------- handler
class Handler(SimpleHTTPRequestHandler):
    server_version = "Caeruleus/2.0"
    protocol_version = "HTTP/1.1"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=APP, **kw)

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # -- helpers
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, x-api-key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Cache-Control", "no-store")

    def _json(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def _raw(self, code, data, ctype="application/octet-stream"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return b""
        return self.rfile.read(n)

    def _json_body(self):
        try:
            return json.loads(self._body().decode("utf-8") or "{}")
        except Exception:
            return {}

    def _path(self):
        return urlparse(self.path).path

    # -- http
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        p = self._path()
        if p == "/api/health":
            return self._json(200, {"ok": True, "version": VERSION, "port": PORT})
        if p == "/api/snapshot":
            snap = kv_all()
            out = {"serverTime": int(time.time() * 1000), "version": VERSION, "items": {}}
            for k, v in snap.items():
                out["items"][k] = v
            return self._json(200, out)
        if p.startswith("/api/oc/"):
            return self._proxy(OC_ORIGIN, p[7:], "GET")
        if p.startswith("/serein") or p.startswith("/mcp"):
            return self._proxy_serein(p, "GET")
        if p.startswith("/store/"):
            name = unquote(p[7:]).strip("/")
            rec = kv_get(name)
            if not rec:
                return self._json(404, {"ok": False, "error": "not found"})
            return self._json(200, rec["data"])
        return super().do_GET()

    def do_POST(self):
        p = self._path()
        if p == "/api/write":
            body = self._json_body()
            name = str(body.get("name") or "").strip()
            if not name:
                return self._json(400, {"ok": False, "error": "name required"})
            data = body.get("data")
            ts = int(body.get("ts") or time.time() * 1000)
            ok, rev = kv_put(name, data, ts)
            return self._json(200, {"ok": ok, "rev": rev, "ts": ts, "stale": not ok})
        if p == "/api/delete":
            body = self._json_body()
            name = str(body.get("name") or "").strip()
            if not name:
                return self._json(400, {"ok": False, "error": "name required"})
            kv_del(name)
            return self._json(200, {"ok": True})
        if p.startswith("/api/oc"):
            return self._proxy(OC_ORIGIN, p[7:] or "/", "POST")
        if p.startswith("/serein") or p.startswith("/mcp"):
            return self._proxy_serein(p, "POST")
        return self._json(404, {"ok": False, "error": "no route"})

    def do_PUT(self):
        p = self._path()
        if p.startswith("/store/"):
            name = unquote(p[7:]).strip("/")
            body = self._body()
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception:
                return self._json(400, {"ok": False, "error": "bad json"})
            ts = int(time.time() * 1000)
            ok, rev = kv_put(name, data, ts)
            return self._json(200, {"ok": ok, "rev": rev, "ts": ts})
        return self._json(404, {"ok": False, "error": "no route"})

    def do_DELETE(self):
        p = self._path()
        if p.startswith("/store/"):
            name = unquote(p[7:]).strip("/")
            kv_del(name)
            return self._json(200, {"ok": True})
        if p.startswith("/api/oc"):
            return self._proxy(OC_ORIGIN, p[7:] or "/", "DELETE")
        return self._json(404, {"ok": False, "error": "no route"})

    # -- proxies
    def _proxy(self, origin, path, method):
        body = None if method in ("GET", "HEAD") else self._body()
        headers = {}
        for k in ("content-type", "accept", "x-opencode-session", "authorization"):
            v = self.headers.get(k)
            if v:
                headers[k] = v
        try:
            st, hdrs, data = relay(origin, path, method, body, headers)
        except Exception as e:
            return self._json(502, {"ok": False, "error": str(e)})
        ctype = hdrs.get("Content-Type") or "application/json"
        self._raw(st, data, ctype)

    def _proxy_serein(self, path, method):
        key = read_key()
        body = None if method in ("GET", "HEAD") else self._body()
        headers = {"content-type": self.headers.get("content-type") or "application/json"}
        if key:
            headers["authorization"] = "Bearer " + key
        real = path
        if path.startswith("/serein/"):
            real = path[len("/serein"):]
        if not real.startswith("/"):
            real = "/" + real
        try:
            st, hdrs, data = relay(SEREIN_ORIGIN, real, method, body, headers)
        except Exception as e:
            return self._json(502, {"ok": False, "error": str(e)})
        ctype = hdrs.get("Content-Type") or "application/json"
        self._raw(st, data, ctype)

    def end_headers(self):
        if self.path.startswith("/") and not self.path.startswith("/api") and not self.path.startswith("/store"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print("[caeruleus] v%s listening on 0.0.0.0:%d  root=%s" % (VERSION, PORT, ROOT), flush=True)
    print("[caeruleus] db=%s  app=%s" % (DB_PATH, APP), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
