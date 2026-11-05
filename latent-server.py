#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Latent 风格共享记忆服务 · 零依赖单文件版
给 Daddy / RikkaHub / Operit 共用一个记忆库。

运行：
    python3 latent-server.py                 # 监听 127.0.0.1:8765
    python3 latent-server.py --host 0.0.0.0 --port 8765
数据目录默认 ~/latent-memory，可用 --data 指定。

HTTP API（JSON）：
    GET  /state                全部状态
    POST /append               写回 {text,current_state,evidence[],unresolvedOps[]}
    POST /supersede            变迁 {recordId,text,current_state,evidence[],unresolvedOps[]}
    POST /correct              撤回 {recordId,reason}
    POST /cleanup              清理 {recordIds[]}
    POST /search               检索 {query,topN}
    POST /unresolved           未解决 {ops[]}
    POST /thread_close         收尾 {summary}
    GET  /session_start        换窗召回
    GET/PUT /persona           人格
    POST /mcp                  MCP JSON-RPC（给 RikkaHub / Operit 用）
"""
import argparse, json, mimetypes, os, re, sys, threading, time, uuid
import urllib.request, urllib.error
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

STATIC_DIR = None
PROXY_BASE = ""

LOCK = threading.Lock()
DATA_DIR = Path.home() / "latent-memory"


def now_ms():
    return int(time.time() * 1000)


def day_of(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def rid():
    return uuid.uuid4().hex[:16]


def fpath(name):
    return DATA_DIR / name


def jload(name, default):
    p = fpath(name)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def jsave(name, obj):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = fpath(name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(fpath(name))


def state():
    return {
        "records": jload("records.json", []),
        "unresolved": jload("unresolved.json", []),
        "threads": jload("threads.json", []),
        "persona": (fpath("persona.md").read_text(encoding="utf-8") if fpath("persona.md").exists() else ""),
    }


def tokens(s):
    s = (s or "").lower()
    out = re.findall(r"[a-z0-9']+", s)
    cn = re.findall(r"[\u4e00-\u9fff]", s)
    out += cn
    out += [cn[i] + cn[i + 1] for i in range(len(cn) - 1)]
    return out


def bm25(query, docs, k1=1.6, b=0.72):
    q = tokens(query)
    if not q or not docs:
        return [0.0] * len(docs)
    qs = set(q)
    df = {t: 0 for t in qs}
    tfs = []
    lens = []
    for d in docs:
        ts = tokens(d)
        lens.append(len(ts))
        f = {}
        for t in ts:
            f[t] = f.get(t, 0) + 1
        tfs.append(f)
        for t in qs:
            if t in f:
                df[t] += 1
    n = len(docs)
    avg = (sum(lens) / n) if n else 1
    scores = []
    for i in range(n):
        s = 0.0
        for t in qs:
            f = tfs[i].get(t, 0)
            if not f:
                continue
            idf = __import__("math").log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * lens[i] / max(avg, 1)))
        scores.append(s)
    return scores


def search(query, topN=5):
    st = state()
    live = [r for r in st["records"] if r.get("status", "active") == "active"]
    if not live:
        return {"ok": True, "items": [], "message": "记忆库还是空的。"}
    texts = [r.get("text", "") + "\n" + r.get("current_state", "") for r in live]
    scores = bm25(query, texts)
    hist = bool(re.search(r"以前|曾经|那时候|搬家前|变迁|当时|过去", query or ""))
    ordered = sorted(range(len(live)), key=lambda i: -scores[i])
    out = []
    seen = set()
    for i in ordered:
        r = live[i]
        if r["id"] in seen:
            continue
        if scores[i] <= 0 and not hist:
            continue
        seen.add(r["id"])
        rr = dict(r)
        rr["score"] = round(scores[i], 3)
        out.append(rr)
        if len(out) >= topN:
            break
    if not out and hist:
        out = [dict(r) for r in sorted(live, key=lambda x: -x.get("ts", 0))[:topN]]
    return {"ok": True, "items": out, "message": "" if out else "记忆库里没有与这个说法相关的记录。"}


def session_start():
    st = state()
    live = [r for r in st["records"] if r.get("status", "active") == "active"]
    live.sort(key=lambda r: (-(r.get("ts", 0)),))
    recent = live[:5]
    unresolved = [u for u in st["unresolved"] if u.get("status") != "closed"]
    thread = st["threads"][-1] if st["threads"] else None
    return {"ok": True, "persona": st["persona"], "unresolved": unresolved,
            "thread": thread, "recent": recent}


def norm_evidence(evidence):
    out = []
    for e in evidence or []:
        if not isinstance(e, dict):
            continue
        t = str(e.get("type") or "event")
        if t not in ("event", "feeling", "reason", "state", "context"):
            t = "event"
        q = str(e.get("quote") or "").strip()
        if q:
            out.append({"type": t, "quote": q})
    return out


def apply_unresolved(ops):
    if not ops:
        return []
    st = state()
    items = st["unresolved"]
    changed = []
    for op in ops:
        if not isinstance(op, dict):
            continue
        act = str(op.get("action") or "none")
        if act == "none":
            continue
        if act == "open":
            it = {"id": rid(), "summary": str(op.get("summary") or "").strip(),
                  "status": "open", "ts": now_ms()}
            if it["summary"]:
                items.append(it)
                changed.append(it)
        elif act == "update":
            for it in items:
                if it["id"] == op.get("id") and op.get("summary"):
                    it["summary"] = str(op["summary"]).strip()
                    changed.append(it)
        elif act == "close":
            for it in items:
                if it["id"] == op.get("id") or (op.get("summary") and it.get("summary") == op["summary"]):
                    it["status"] = "closed"
                    changed.append(it)
    jsave("unresolved.json", items)
    return changed


def append(payload, supersede_id=None):
    text = str(payload.get("text") or "").strip()
    cur = str(payload.get("current_state") or "").strip()
    if not text:
        raise ValueError("text 不能为空")
    ev = norm_evidence(payload.get("indexEvidence") or payload.get("evidence"))
    st = state()
    recs = st["records"]
    old = None
    if supersede_id:
        for r in recs:
            if r["id"] == supersede_id:
                old = r
                break
        if not old:
            raise ValueError("找不到被取代的记录")
    if not old:
        for r in recs:
            if r.get("status") == "active" and r.get("text") == text:
                if cur:
                    r["current_state"] = cur
                r["ts"] = now_ms()
                r["day"] = day_of(r["ts"])
                r["weight"] = round(min(3.0, float(r.get("weight") or 1) + 0.05), 3)
                jsave("records.json", recs)
                return {"ok": True, "record": r, "dedup": True}
    rec = {
        "id": rid(), "ts": now_ms(), "day": day_of(now_ms()),
        "text": text, "current_state": cur, "evidence": ev,
        "status": "active", "supersedes": supersede_id or "", "supersededBy": "",
        "weight": 1.0, "hits": 0, "retractedReason": "",
    }
    if old:
        old["status"] = "superseded"
        old["supersededBy"] = rec["id"]
    recs.append(rec)
    jsave("records.json", recs)
    changed = apply_unresolved(payload.get("unresolvedOps"))
    return {"ok": True, "record": rec, "superseded": old["id"] if old else "", "unresolved": changed}


def correct(record_id, reason):
    st = state()
    for r in st["records"]:
        if r["id"] == record_id:
            r["status"] = "retracted"
            r["retractedReason"] = str(reason or "")
            jsave("records.json", st["records"])
            return {"ok": True, "record": r}
    raise ValueError("找不到这条记录")


def cleanup(ids):
    st = state()
    before = len(st["records"])
    st["records"] = [r for r in st["records"] if r["id"] not in set(ids or [])]
    jsave("records.json", st["records"])
    return {"ok": True, "removed": before - len(st["records"])}


def thread_close(summary):
    st = state()
    t = {"ts": now_ms(), "day": day_of(now_ms()), "summary": str(summary or "").strip()}
    st["threads"].append(t)
    jsave("threads.json", st["threads"])
    return {"ok": True, "thread": t}


# ---------- MCP（Streamable HTTP 子集） ----------
TOOLS = [
    {"name": "latent_session_start", "description": "新会话开场：返回未解决、上次会话线索、最近记忆。无参数。",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "latent_search", "description": "按关键词检索记忆。",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "topN": {"type": "integer"}}, "required": ["query"]}},
    {"name": "latent_append", "description": "写回一条记忆。",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string"}, "current_state": {"type": "string"},
         "indexEvidence": {"type": "array", "items": {"type": "object"}},
         "unresolvedOps": {"type": "array", "items": {"type": "object"}}}, "required": ["text"]}},
    {"name": "latent_supersede", "description": "事实变化：新记录取代旧记录，保留变迁链。",
     "inputSchema": {"type": "object", "properties": {
         "supersedes": {"type": "string"}, "text": {"type": "string"},
         "current_state": {"type": "string"}, "indexEvidence": {"type": "array", "items": {"type": "object"}}},
         "required": ["supersedes", "text"]}},
    {"name": "latent_correct", "description": "撤回记错的内容。",
     "inputSchema": {"type": "object", "properties": {"recordId": {"type": "string"}, "reason": {"type": "string"}}, "required": ["recordId"]}},
    {"name": "latent_cleanup", "description": "彻底删除指定记录。",
     "inputSchema": {"type": "object", "properties": {"recordIds": {"type": "array", "items": {"type": "string"}}}, "required": ["recordIds"]}},
    {"name": "latent_unresolved", "description": "维护未解决清单。",
     "inputSchema": {"type": "object", "properties": {"ops": {"type": "array", "items": {"type": "object"}}}, "required": ["ops"]}},
    {"name": "latent_thread_close", "description": "窗口结束时留下会话快照。",
     "inputSchema": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}},
]
TOOL_NAMES = {t["name"] for t in TOOLS}


def text_result(text):
    return {"content": [{"type": "text", "text": text}], "isError": False}


def call_tool(name, args):
    if name == "latent_session_start":
        s = session_start()
        parts = []
        if s["unresolved"]:
            parts.append("【当前未解决】\n" + "\n".join("- " + u["summary"] for u in s["unresolved"]))
        if s["thread"]:
            parts.append("【上次会话】\n" + s["thread"]["summary"])
        if s["recent"]:
            parts.append("【最近记忆】\n" + "\n".join("- (" + r["day"] + ") " + r["text"][:120] for r in s["recent"]))
        return text_result("\n\n".join(parts) or "记忆库还是空的。")
    if name == "latent_search":
        s = search(args.get("query", ""), int(args.get("topN") or 5))
        if not s["items"]:
            return {"content": [{"type": "text", "text": "没有可靠命中。" + s["message"]}], "isError": True}
        return text_result("\n\n".join("- (" + r["day"] + ") " + r["text"] + ("\n  现状：" + r["current_state"] if r["current_state"] else "") for r in s["items"]))
    if name == "latent_append":
        r = append(args)
        return text_result("已写入 recordId=" + r["record"]["id"])
    if name == "latent_supersede":
        r = append(args, supersede_id=args.get("supersedes"))
        return text_result("已取代 recordId=" + r["record"]["id"] + " (旧 " + r["superseded"] + ")")
    if name == "latent_correct":
        r = correct(args.get("recordId"), args.get("reason"))
        return text_result("已撤回 recordId=" + r["record"]["id"])
    if name == "latent_cleanup":
        r = cleanup(args.get("recordIds"))
        return text_result("已删除 " + str(r["removed"]) + " 条")
    if name == "latent_unresolved":
        changed = apply_unresolved(args.get("ops"))
        return text_result("未解决清单已更新 " + str(len(changed)) + " 项")
    if name == "latent_thread_close":
        r = thread_close(args.get("summary"))
        return text_result("已留下会话线索。")
    return {"content": [{"type": "text", "text": "未知工具 " + name}], "isError": True}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LatentMemory/1.0"

    def log_message(self, fmt, *args):
        pass

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept, Mcp-Session-Id, Authorization")
        self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id")

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def serve_static(self, p):
        if STATIC_DIR is None:
            return False
        rel = (p or "/").lstrip("/") or "index.html"
        try:
            fp = (STATIC_DIR / rel).resolve()
            if not str(fp).startswith(str(STATIC_DIR)):
                return False
            if fp.is_dir():
                fp = fp / "index.html"
            if not fp.is_file():
                return False
            data = fp.read_bytes()
        except Exception:
            return False
        ctype = mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.cors()
        self.end_headers()
        self.wfile.write(data)
        return True

    def proxy_api(self, p, q=""):
        if not PROXY_BASE:
            return False
        if p == "/api/proxy":
            from urllib.parse import parse_qs
            sub = (parse_qs(q).get("p") or ["/"])[0]
            url = PROXY_BASE.rstrip("/") + sub
        else:
            url = PROXY_BASE.rstrip("/") + p[len("/api"):]
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else None
        req = urllib.request.Request(url, data=body, method=self.command)
        req.add_header("User-Agent", "Mozilla/5.0 (Linux; Android 14; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36")
        for h in ("Content-Type", "Authorization", "Accept", "x-opencode-session"):
            v = self.headers.get(h)
            if v:
                req.add_header(h, v)
        try:
            resp = urllib.request.urlopen(req, timeout=180)
        except urllib.error.HTTPError as e:
            resp = e
        except Exception as e:
            return self.send_json({"error": str(e)}, 502)
        try:
            code = resp.status
        except Exception:
            code = 200
        self.send_response(code)
        ct = resp.headers.get("Content-Type") or "application/json"
        self.send_header("Content-Type", ct)
        self.send_header("Cache-Control", "no-cache")
        self.cors()
        clen = resp.headers.get("Content-Length")
        if clen:
            self.send_header("Content-Length", clen)
        else:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        try:
            while True:
                chunk = resp.read(2048)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception:
            pass
        return True

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p.startswith("/api/"):
            if self.proxy_api(p, u.query):
                return
        if p == "/health":
            return self.send_json({"ok": True, "service": "latent-memory", "records": len(state()["records"])})
        if p == "/state":
            return self.send_json(state())
        if p == "/session_start":
            return self.send_json(session_start())
        if p == "/persona":
            return self.send_json({"persona": state()["persona"]})
        if self.serve_static(p):
            return
        return self.send_json({"ok": False, "error": "not found"}, 404)

    def do_PUT(self):
        u = urlparse(self.path)
        if u.path == "/persona":
            body = self.read_json()
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            fpath("persona.md").write_text(str(body.get("persona") or ""), encoding="utf-8")
            return self.send_json({"ok": True})
        return self.send_json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        if p.startswith("/api/"):
            if self.proxy_api(p, u.query):
                return
        body = self.read_json()
        try:
            if p == "/mcp":
                return self.handle_mcp(body)
            if p == "/append":
                return self.send_json(append(body))
            if p == "/supersede":
                return self.send_json(append(body, supersede_id=body.get("supersedes") or body.get("recordId")))
            if p == "/correct":
                return self.send_json(correct(body.get("recordId"), body.get("reason")))
            if p == "/cleanup":
                return self.send_json(cleanup(body.get("recordIds")))
            if p == "/search":
                return self.send_json(search(body.get("query", ""), int(body.get("topN") or 5)))
            if p == "/unresolved":
                return self.send_json({"ok": True, "changed": apply_unresolved(body.get("ops"))})
            if p == "/thread_close":
                return self.send_json(thread_close(body.get("summary")))
            if p == "/session_start":
                return self.send_json(session_start())
            return self.send_json({"ok": False, "error": "not found"}, 404)
        except ValueError as e:
            return self.send_json({"ok": False, "error": str(e)}, 400)
        except Exception as e:
            return self.send_json({"ok": False, "error": type(e).__name__ + ": " + str(e)}, 500)

    def handle_mcp(self, body):
        method = body.get("method")
        mid = body.get("id")
        if method == "initialize":
            res = {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "latent-memory", "version": "1.0"}}}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Mcp-Session-Id", uuid.uuid4().hex)
            self.send_header("Content-Length", str(len(json.dumps(res))))
            self.cors()
            self.end_headers()
            self.wfile.write(json.dumps(res).encode())
            return
        if method == "notifications/initialized":
            self.send_response(202)
            self.cors()
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "tools/list":
            return self.send_json({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        if method == "tools/call":
            params = body.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name not in TOOL_NAMES:
                return self.send_json({"jsonrpc": "2.0", "id": mid,
                                       "error": {"code": -32601, "message": "未知工具 " + str(name)}})
            try:
                r = call_tool(name, args)
                return self.send_json({"jsonrpc": "2.0", "id": mid, "result": r})
            except Exception as e:
                return self.send_json({"jsonrpc": "2.0", "id": mid, "result":
                                       {"content": [{"type": "text", "text": "错误：" + str(e)}], "isError": True}})
        if method == "ping":
            return self.send_json({"jsonrpc": "2.0", "id": mid, "result": {}})
        return self.send_json({"jsonrpc": "2.0", "id": mid,
                               "error": {"code": -32601, "message": "未实现 " + str(method)}})


def main():
    global DATA_DIR, STATIC_DIR, PROXY_BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--data", default=str(Path.home() / "latent-memory"))
    ap.add_argument("--static", default="", help="静态文件目录（给 Daddy 页面用）")
    ap.add_argument("--proxy", default="", help="把 /api/* 转发到这个上游")
    a = ap.parse_args()
    DATA_DIR = Path(a.data)
    if a.static:
        STATIC_DIR = Path(a.static).resolve()
    PROXY_BASE = a.proxy
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print("Latent memory server on http://%s:%d  (data: %s)" % (a.host, a.port, DATA_DIR))
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
