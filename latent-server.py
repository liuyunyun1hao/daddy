#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Latent 共享记忆服务 · SQLite 单写者版（零第三方依赖）
给 Daddy / RikkaHub / Operit 共用一个记忆库。

设计要点：
  * SQLite 是唯一真相源（WAL + 单写事务），JSON 文件只用于首次迁移。
  * 所有客户端写操作走 POST /memory/op，op_id 幂等，rev 单调递增。
  * 客户端用 GET /memory/snapshot?since_rev= 做增量同步；GET /memory/recall 做服务端检索。
  * 旧接口 /state /append /supersede /correct /cleanup /search /unresolved /thread_close
    全部保留，内部转成同一套 op。
  * MCP（Streamable HTTP 子集）保留并新增 latent_remember / latent_recall / latent_resolve / latent_forget / latent_snapshot。

运行：
    python3 latent-server.py                 # 127.0.0.1:8765，token 自动生成并强制校验
    python3 latent-server.py --lan           # 0.0.0.0:8765，同样要 token
    python3 latent-server.py --token XXX     # 自己指定 token
    python3 latent-server.py --no-token      # 明确关闭鉴权（只建议本机调试）
数据目录默认 ~/latent-memory，可用 --data 指定。

鉴权：首次启动在数据目录生成 token（权限 0600），并打印配对链接
    http://127.0.0.1:8765/#t=<token>
页面读到 #t= 后把 token 存进 localStorage 并清掉地址栏；
其他客户端（MCP / Operit / RikkaHub）用 X-Latent-Token 或 Bearer。
为什么默认强制：Android 上 127.0.0.1 是所有 App 共享的，"本机"证明不了是谁。

备份：数据目录/backups 下每 6 小时自动一份（VACUUM INTO，WAL 下也一致），保留 14 份。
"""
import argparse
import hmac
import json
import math
import mimetypes
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import uuid
import urllib.request
import urllib.error
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

STATIC_DIR = None
PROXY_BASE = ""
TOKEN = ""
DB_PATH = None
LEGACY_DIR = Path.home() / "latent-memory"
MAX_BODY = 8 * 1024 * 1024
STATIC_EXTS = {".html", ".htm", ".js", ".mjs", ".css", ".json", ".webmanifest",
               ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
               ".woff", ".woff2", ".ttf", ".mp3", ".wav"}
# 静态目录里这些一律不对外：源码、脚本、文档、旧版本、备份、数据库
STATIC_DENY_EXT = {".py", ".sh", ".md", ".txt", ".db", ".sqlite", ".bak", ".log"}
STATIC_DENY_NAME = {"__pycache__"}
BACKUP_DIR = None
BACKUP_KEEP = 14
BACKUP_EVERY = 6 * 3600
LOCK = threading.RLock()
WRITE_LOCK = threading.RLock()


def now_ms():
    return int(time.time() * 1000)


def day_of(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def rid():
    return uuid.uuid4().hex[:16]


# ---------------- SQLite ----------------

def connect():
    conn = sqlite3.connect(str(DB_PATH), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            scope TEXT NOT NULL DEFAULT 'main',
            kind TEXT NOT NULL DEFAULT 'fact',
            topic TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL,
            current_state TEXT NOT NULL DEFAULT '',
            evidence TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'active',
            supersedes TEXT NOT NULL DEFAULT '',
            superseded_by TEXT NOT NULL DEFAULT '',
            weight REAL NOT NULL DEFAULT 1,
            hits INTEGER NOT NULL DEFAULT 0,
            retracted_reason TEXT NOT NULL DEFAULT '',
            created_ts INTEGER NOT NULL,
            updated_ts INTEGER NOT NULL,
            rev INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_mem_rev ON memories(rev);
        CREATE INDEX IF NOT EXISTS idx_mem_status ON memories(status);
        CREATE INDEX IF NOT EXISTS idx_mem_topic ON memories(topic);
        CREATE TABLE IF NOT EXISTS working_state (
            scope TEXT PRIMARY KEY,
            state_text TEXT NOT NULL DEFAULT '',
            updated_ts INTEGER NOT NULL,
            rev INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_ws_rev ON working_state(rev);
        CREATE TABLE IF NOT EXISTS unresolved (
            id TEXT PRIMARY KEY,
            scope TEXT NOT NULL DEFAULT 'main',
            summary TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open',
            ts INTEGER NOT NULL,
            rev INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_unres_rev ON unresolved(rev);
        CREATE TABLE IF NOT EXISTS threads (
            id TEXT PRIMARY KEY,
            scope TEXT NOT NULL DEFAULT 'main',
            summary TEXT NOT NULL DEFAULT '',
            ts INTEGER NOT NULL,
            day TEXT NOT NULL DEFAULT '',
            closed INTEGER NOT NULL DEFAULT 0,
            rev INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_thread_rev ON threads(rev);
        CREATE TABLE IF NOT EXISTS ops (
            op_id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            base_rev INTEGER NOT NULL DEFAULT 0,
            payload TEXT NOT NULL DEFAULT '{}',
            result TEXT NOT NULL DEFAULT '{}',
            created_ts INTEGER NOT NULL
        );
        """)
        # 旧库迁移：memories 增加 scope 列（默认 main），并补索引。
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(memories)")]
        if "scope" not in cols:
            conn.execute("ALTER TABLE memories ADD COLUMN scope TEXT NOT NULL DEFAULT 'main'")
        for t in ("unresolved", "threads"):
            tcols = [r["name"] for r in conn.execute("PRAGMA table_info(" + t + ")")]
            if "scope" not in tcols:
                conn.execute("ALTER TABLE " + t + " ADD COLUMN scope TEXT NOT NULL DEFAULT 'main'")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_unres_scope ON unresolved(scope, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_thread_scope ON threads(scope, ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_scope ON memories(scope, topic, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_scope_rev ON memories(scope, rev)")
        if conn.execute("SELECT v FROM meta WHERE k='rev'").fetchone() is None:
            conn.execute("INSERT INTO meta(k,v) VALUES('rev','0')")
        conn.execute("INSERT OR IGNORE INTO meta(k,v) VALUES('schema','3')")
        # ops 是幂等回执表：只留最近 30 天，避免长期使用后无限膨胀
        try:
            conn.execute("DELETE FROM ops WHERE created_ts < ?", (now_ms() - 30 * 86400000,))
        except Exception:
            pass
    finally:
        conn.close()


def get_rev(conn):
    row = conn.execute("SELECT v FROM meta WHERE k='rev'").fetchone()
    return int(row["v"]) if row else 0


def bump_rev(conn):
    rev = get_rev(conn) + 1
    conn.execute("UPDATE meta SET v=? WHERE k='rev'", (str(rev),))
    return rev


# ---------------- token / 备份 ----------------

def token_path():
    return DB_PATH.parent / "token"


def read_or_create_token():
    """返回 (token, 是否新建)。文件 0600，只有 Termux 自己能读。"""
    p = token_path()
    try:
        tok = p.read_text(encoding="utf-8").strip()
    except Exception:
        tok = ""
    if len(tok) >= 16:
        return tok, False
    tok = secrets.token_urlsafe(24)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(tok + "\n")
        os.chmod(str(p), 0o600)
    except Exception as e:
        print("WARNING: 无法写入 token 文件 %s (%s)" % (p, e), file=sys.stderr)
    return tok, True


def backup_list():
    items = []
    if not BACKUP_DIR or not BACKUP_DIR.is_dir():
        return items
    for fp in BACKUP_DIR.glob("latent-*.db"):
        try:
            st = fp.stat()
        except OSError:
            continue
        items.append({"name": fp.name, "size": st.st_size, "ts": int(st.st_mtime * 1000)})
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items


def make_backup(tag="manual"):
    """VACUUM INTO 做一致性快照：WAL 下也能得到完整可用的库。"""
    if not BACKUP_DIR:
        raise RuntimeError("backup disabled")
    if not DB_PATH or not DB_PATH.is_file():
        raise RuntimeError("no database")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = "latent-%s.db" % stamp
    tmp = BACKUP_DIR / (".tmp-" + name)
    target = BACKUP_DIR / name
    try:
        tmp.unlink()
    except OSError:
        pass
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    try:
        conn.execute("VACUUM INTO '%s'" % str(tmp).replace("'", "''"))
    finally:
        conn.close()
    os.replace(str(tmp), str(target))
    for old in backup_list()[BACKUP_KEEP:]:
        try:
            (BACKUP_DIR / old["name"]).unlink()
        except OSError:
            pass
    return {"name": name, "size": target.stat().st_size, "tag": tag}


def backup_tick():
    """后台每小时看一眼：超过 BACKUP_EVERY 秒没有新备份就补一份。"""
    if not BACKUP_DIR:
        return
    try:
        items = backup_list()
        if not items or (time.time() * 1000 - items[0]["ts"]) > BACKUP_EVERY * 1000:
            make_backup("auto")
    except Exception as e:
        print("WARNING: 自动备份失败：%s" % e, file=sys.stderr)


def backup_loop():
    while True:
        backup_tick()
        time.sleep(3600)


def row_mem(r):
    try:
        ev = json.loads(r["evidence"] or "[]")
    except Exception:
        ev = []
    return {
        "id": r["id"], "scope": r["scope"], "kind": r["kind"], "topic": r["topic"],
        "ts": r["created_ts"], "day": day_of(r["created_ts"]),
        "text": r["text"], "current_state": r["current_state"], "evidence": ev,
        "status": r["status"], "supersedes": r["supersedes"],
        "supersededBy": r["superseded_by"], "weight": r["weight"], "hits": r["hits"],
        "retractedReason": r["retracted_reason"], "rev": r["rev"],
        "updatedAt": r["updated_ts"],
    }


def row_working(r):
    return {"scope": r["scope"], "state_text": r["state_text"],
            "updated_ts": r["updated_ts"], "rev": r["rev"],
            "day": day_of(r["updated_ts"])}


def norm_scope(s):
    s = str(s or "").strip()
    if not s:
        return "main"
    s = re.sub(r"[\x00-\x1f]", "", s)
    return s[:64]


def row_unres(r):
    return {"id": r["id"], "scope": r["scope"] if "scope" in r.keys() else "main",
            "summary": r["summary"], "status": r["status"],
            "ts": r["ts"], "rev": r["rev"]}


def row_thread(r):
    return {"id": r["id"], "scope": r["scope"] if "scope" in r.keys() else "main",
            "summary": r["summary"], "ts": r["ts"], "day": r["day"],
            "closed": bool(r["closed"]), "rev": r["rev"]}


def q_persona(conn):
    row = conn.execute("SELECT v FROM meta WHERE k='persona'").fetchone()
    return row["v"] if row else ""


def set_persona(text):
    """共享人格写入口：PUT /persona 与 POST /persona 共用，返回新 rev。"""
    conn = connect()
    try:
        with WRITE_LOCK:
            conn.execute("BEGIN IMMEDIATE")
            rev = bump_rev(conn)
            conn.execute("INSERT INTO meta(k,v) VALUES('persona',?) "
                         "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                         (str(text or ""),))
            conn.execute("INSERT INTO meta(k,v) VALUES('persona_rev',?) "
                         "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(rev),))
            conn.execute("COMMIT")
        return rev
    finally:
        conn.close()


def q_snapshot(conn, since_rev=0, limit=100000, scope=None):
    rev = get_rev(conn)
    if scope:
        mem = conn.execute(
            "SELECT * FROM memories WHERE rev>? AND scope=? ORDER BY rev ASC LIMIT ?",
            (int(since_rev or 0), norm_scope(scope), int(limit))).fetchall()
    else:
        mem = conn.execute(
            "SELECT * FROM memories WHERE rev>? ORDER BY rev ASC LIMIT ?",
            (int(since_rev or 0), int(limit))).fetchall()
    if scope:
        unres = conn.execute(
            "SELECT * FROM unresolved WHERE rev>? AND scope=? ORDER BY rev ASC LIMIT ?",
            (int(since_rev or 0), norm_scope(scope), int(limit))).fetchall()
        th = conn.execute(
            "SELECT * FROM threads WHERE rev>? AND scope=? ORDER BY rev ASC LIMIT ?",
            (int(since_rev or 0), norm_scope(scope), int(limit))).fetchall()
    else:
        unres = conn.execute(
            "SELECT * FROM unresolved WHERE rev>? ORDER BY rev ASC LIMIT ?",
            (int(since_rev or 0), int(limit))).fetchall()
        th = conn.execute(
            "SELECT * FROM threads WHERE rev>? ORDER BY rev ASC LIMIT ?",
            (int(since_rev or 0), int(limit))).fetchall()
    if scope:
        ws = conn.execute(
            "SELECT * FROM working_state WHERE rev>? AND scope=? ORDER BY rev ASC LIMIT ?",
            (int(since_rev or 0), norm_scope(scope), int(limit))).fetchall()
    else:
        ws = conn.execute(
            "SELECT * FROM working_state WHERE rev>? ORDER BY rev ASC LIMIT ?",
            (int(since_rev or 0), int(limit))).fetchall()
    return {"ok": True, "rev": rev,
            "records": [row_mem(r) for r in mem],
            "unresolved": [row_unres(r) for r in unres],
            "threads": [row_thread(r) for r in th],
            "states": [row_working(r) for r in ws],
            "persona": q_persona(conn)}


def q_state(conn):
    """旧接口：全量状态（含历史记录）。"""
    mem = conn.execute("SELECT * FROM memories ORDER BY created_ts ASC").fetchall()
    unres = conn.execute("SELECT * FROM unresolved ORDER BY ts ASC").fetchall()
    th = conn.execute("SELECT * FROM threads ORDER BY ts ASC").fetchall()
    ws = conn.execute("SELECT * FROM working_state ORDER BY updated_ts ASC").fetchall()
    return {"records": [row_mem(r) for r in mem],
            "unresolved": [row_unres(r) for r in unres],
            "threads": [row_thread(r) for r in th],
            "states": [row_working(r) for r in ws],
            "persona": q_persona(conn)}


# ---------------- 检索 ----------------

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
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * lens[i] / max(avg, 1)))
        scores.append(s)
    return scores


HIST_RE = re.compile(r"以前|曾经|那时候|搬家前|变迁|当时|过去|原来|之前")


def cjk_bigrams(s):
    out = set()
    for m in re.finditer(r"[\u4e00-\u9fff]+", str(s or "")):
        run = m.group(0)
        for i in range(len(run) - 1):
            out.add(run[i:i+2])
    return out


def rank_records(conn, query, top_n=6, scope=None):
    if scope:
        rows = conn.execute(
            "SELECT * FROM memories WHERE status='active' AND scope=? ORDER BY created_ts DESC",
            (norm_scope(scope),)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM memories WHERE status='active' ORDER BY created_ts DESC").fetchall()
    if not rows:
        return [], "记忆库还是空的。"
    # 中文按字匹配会把「的/了/在」这类高频字全算命中；有二元词时要求至少命中一个二元词
    qb = cjk_bigrams(query)
    if qb:
        rows = [r for r in rows if any(b in (r["text"] + "\n" + (r["current_state"] or "")) for b in qb)]
        if not rows:
            return [], "记忆库里没有与这个说法相关的记录。"
    docs = [r["text"] + "\n" + r["current_state"] for r in rows]
    scores = bm25(query, docs)
    now = now_ms()
    hist = bool(HIST_RE.search(query or ""))
    scored = []
    for i, r in enumerate(rows):
        # 只有真正命中查询词的记录才参与排序；否则最近但无关的记忆会挤进召回
        if scores[i] <= 0 and not hist:
            continue
        age_days = max(0.0, (now - r["created_ts"]) / 86400000.0)
        s = scores[i] + math.exp(-age_days / 45.0) * 0.6 + float(r["weight"] or 1) * 0.3
        scored.append((s, r))
    scored.sort(key=lambda x: -x[0])
    out = []
    seen = set()
    for s, r in scored:
        if s <= 0 and not hist:
            continue
        key = (r["text"], r["current_state"] or "")
        if key in seen:
            continue
        seen.add(key)
        d = row_mem(r)
        d["score"] = round(s, 3)
        out.append(d)
        if len(out) >= top_n:
            break
    if not out and hist:
        for r in rows[:top_n]:
            d = row_mem(r)
            d["score"] = 0
            out.append(d)
    return out, ("" if out else "记忆库里没有与这个说法相关的记录。")


# ---------------- 写操作 ----------------

KINDS = {"fact", "event", "promise", "clue", "pref", "unresolved", "state"}


def norm_kind(k):
    k = str(k or "fact").strip().lower()
    return k if k in KINDS else "fact"


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


def apply_ops(conn, ops, rev, device_id="", session_id=""):
    """在已有事务中执行一组 op，返回结果列表。"""
    results = []
    for op in ops or []:
        if not isinstance(op, dict):
            continue
        typ = str(op.get("type") or op.get("op") or "").strip()
        r = {"type": typ}
        if typ in ("remember", "append"):
            text = str(op.get("text") or "").strip()
            if not text:
                r.update({"ok": False, "error": "text 不能为空"})
                results.append(r)
                continue
            kind = norm_kind(op.get("kind"))
            scope = norm_scope(op.get("scope"))
            topic = str(op.get("topic") or "").strip()
            cur = str(op.get("current_state") or "").strip()
            ev = norm_evidence(op.get("evidence") or op.get("indexEvidence"))
            # 1) 完全相同的 active 文本：更新而不是新增（同 scope 内）
            dup = conn.execute(
                "SELECT * FROM memories WHERE status='active' AND scope=? AND text=? ORDER BY created_ts DESC LIMIT 1",
                (scope, text)).fetchone()
            if dup is not None:
                ts = now_ms()
                conn.execute(
                    "UPDATE memories SET current_state=?, weight=?, updated_ts=?, rev=? WHERE id=?",
                    (cur or dup["current_state"], round(min(3.0, float(dup["weight"]) + 0.05), 3),
                     ts, rev, dup["id"]))
                r.update({"ok": True, "id": dup["id"], "dedup": True, "status": "active"})
                results.append(r)
                continue
            # 2) 同 scope + 同 topic 的旧 active 记录被取代
            old = None
            if topic:
                old = conn.execute(
                    "SELECT * FROM memories WHERE status='active' AND scope=? AND topic=? AND id<>? ORDER BY created_ts DESC LIMIT 1",
                    (scope, topic, "")).fetchone()
            # 3) 显式 supersedes
            sup = str(op.get("supersedes") or "").strip()
            if sup:
                old = conn.execute("SELECT * FROM memories WHERE id=?", (sup,)).fetchone() or old
            new_id = rid()
            ts = now_ms()
            conn.execute(
                "INSERT INTO memories(id,scope,kind,topic,text,current_state,evidence,status,"
                "supersedes,superseded_by,weight,hits,retracted_reason,created_ts,updated_ts,rev)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id, scope, kind, topic, text, cur, json.dumps(ev, ensure_ascii=False),
                 "active", old["id"] if old else "", "", 1.0, 0, "", ts, ts, rev))
            if old is not None:
                conn.execute(
                    "UPDATE memories SET status='superseded', superseded_by=?, updated_ts=?, rev=? WHERE id=?",
                    (new_id, ts, rev, old["id"]))
            r.update({"ok": True, "id": new_id, "status": "active",
                      "scope": scope, "superseded": old["id"] if old else ""})
            results.append(r)
        elif typ == "supersede":
            old_id = str(op.get("id") or op.get("supersedes") or "").strip()
            text = str(op.get("text") or "").strip()
            if not old_id or not text:
                r.update({"ok": False, "error": "supersede 需要 id 和 text"})
                results.append(r)
                continue
            old = conn.execute("SELECT * FROM memories WHERE id=?", (old_id,)).fetchone()
            if old is None:
                r.update({"ok": False, "error": "找不到被取代的记录"})
                results.append(r)
                continue
            new_id = rid()
            ts = now_ms()
            cur = str(op.get("current_state") or "").strip()
            ev = norm_evidence(op.get("evidence") or op.get("indexEvidence"))
            conn.execute(
                "INSERT INTO memories(id,scope,kind,topic,text,current_state,evidence,status,"
                "supersedes,superseded_by,weight,hits,retracted_reason,created_ts,updated_ts,rev)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id, old["scope"], old["kind"], old["topic"], text, cur,
                 json.dumps(ev, ensure_ascii=False), "active", old_id, "",
                 1.0, 0, "", ts, ts, rev))
            conn.execute(
                "UPDATE memories SET status='superseded', superseded_by=?, updated_ts=?, rev=? WHERE id=?",
                (new_id, ts, rev, old_id))
            r.update({"ok": True, "id": new_id, "superseded": old_id})
            results.append(r)
        elif typ == "correct":
            rid_ = str(op.get("id") or op.get("recordId") or "").strip()
            reason = str(op.get("reason") or "").strip()
            if not rid_:
                r.update({"ok": False, "error": "correct 需要 id"})
                results.append(r)
                continue
            cscope = norm_scope(op.get("scope")) if op.get("scope") else ""
            if cscope:
                cur = conn.execute(
                    "UPDATE memories SET status='retracted', retracted_reason=?, updated_ts=?, rev=?"
                    " WHERE id=? AND scope=?",
                    (reason, now_ms(), rev, rid_, cscope))
            else:
                cur = conn.execute(
                    "UPDATE memories SET status='retracted', retracted_reason=?, updated_ts=?, rev=? WHERE id=?",
                    (reason, now_ms(), rev, rid_))
            r.update({"ok": cur.rowcount > 0, "id": rid_, "status": "retracted"})
            if cur.rowcount == 0:
                r["error"] = "找不到这条记录"
            results.append(r)
        elif typ in ("forget", "cleanup"):
            ids = op.get("ids")
            if isinstance(ids, str):
                ids = [ids]
            if not ids:
                ids = [op.get("id") or op.get("recordId")] if (op.get("id") or op.get("recordId")) else []
            topic = str(op.get("topic") or "").strip()
            about = str(op.get("about") or "").strip()
            fscope = norm_scope(op.get("scope")) if op.get("scope") else ""
            targets = set(x for x in (ids or []) if x)
            if topic:
                if fscope:
                    for row in conn.execute("SELECT id FROM memories WHERE status='active' AND scope=? AND topic=?",
                                            (fscope, topic)):
                        targets.add(row["id"])
                else:
                    for row in conn.execute("SELECT id FROM memories WHERE status='active' AND topic=?", (topic,)):
                        targets.add(row["id"])
            if about:
                if fscope:
                    for row in conn.execute("SELECT id FROM memories WHERE status='active' AND scope=? AND text LIKE ?",
                                            (fscope, "%" + about + "%")):
                        targets.add(row["id"])
                else:
                    for row in conn.execute("SELECT id FROM memories WHERE status='active' AND text LIKE ?", ("%" + about + "%",)):
                        targets.add(row["id"])
            n = 0
            for i in targets:
                cur = conn.execute(
                    "UPDATE memories SET status='deleted', updated_ts=?, rev=? WHERE id=? AND status<>'deleted'",
                    (now_ms(), rev, i))
                n += cur.rowcount
            r.update({"ok": True, "removed": n, "ids": sorted(targets)})
            results.append(r)
        elif typ == "resolve":
            rid_ = str(op.get("id") or "").strip()
            summary = str(op.get("summary") or "").strip()
            topic = str(op.get("topic") or "").strip()
            n = 0
            rscope0 = norm_scope(op.get("scope")) if op.get("scope") else ""
            if rid_:
                if rscope0:
                    cur = conn.execute(
                        "UPDATE unresolved SET status='closed', rev=? WHERE id=? AND scope=? AND status<>'closed'",
                        (rev, rid_, rscope0))
                else:
                    cur = conn.execute(
                        "UPDATE unresolved SET status='closed', rev=? WHERE id=? AND status<>'closed'",
                        (rev, rid_))
                n += cur.rowcount
                cur = conn.execute(
                    "UPDATE memories SET status='closed', updated_ts=?, rev=? WHERE id=? AND status='active'",
                    (now_ms(), rev, rid_))
                n += cur.rowcount
            if summary:
                if rscope0:
                    cur = conn.execute(
                        "UPDATE unresolved SET status='closed', rev=? WHERE summary=? AND scope=? AND status<>'closed'",
                        (rev, summary, rscope0))
                else:
                    cur = conn.execute(
                        "UPDATE unresolved SET status='closed', rev=? WHERE summary=? AND status<>'closed'",
                        (rev, summary))
                n += cur.rowcount
            if topic:
                rscope = norm_scope(op.get("scope")) if op.get("scope") else ""
                if rscope:
                    cur = conn.execute(
                        "UPDATE memories SET status='closed', updated_ts=?, rev=?"
                        " WHERE topic=? AND scope=? AND status='active'",
                        (now_ms(), rev, topic, rscope))
                else:
                    cur = conn.execute(
                        "UPDATE memories SET status='closed', updated_ts=?, rev=? WHERE topic=? AND status='active'",
                        (now_ms(), rev, topic))
                n += cur.rowcount
            r.update({"ok": True, "closed": n})
            results.append(r)
        elif typ == "unresolved":
            # 兼容旧接口：action open/update/close
            act = str(op.get("action") or "none")
            rid_ = str(op.get("id") or "").strip()
            summary = str(op.get("summary") or "").strip()
            uscope = norm_scope(op.get("scope"))
            if act == "open":
                if rid_:
                    cur = conn.execute(
                        "UPDATE unresolved SET summary=CASE WHEN ?<>'' THEN ? ELSE summary END,"
                        " status='open', ts=?, rev=?, scope=? WHERE id=?",
                        (summary, summary, now_ms(), rev, uscope, rid_))
                    if cur.rowcount == 0 and summary:
                        conn.execute("INSERT INTO unresolved(id,scope,summary,status,ts,rev) VALUES(?,?,?,?,?,?)",
                                     (rid_, uscope, summary, "open", now_ms(), rev))
                elif summary:
                    conn.execute("INSERT INTO unresolved(id,scope,summary,status,ts,rev) VALUES(?,?,?,?,?,?)",
                                 (rid(), uscope, summary, "open", now_ms(), rev))
            elif act == "update" and rid_ and summary:
                conn.execute("UPDATE unresolved SET summary=?, rev=?, scope=? WHERE id=?",
                             (summary, rev, uscope, rid_))
            elif act == "close":
                if rid_:
                    conn.execute("UPDATE unresolved SET status='closed', rev=?, scope=? WHERE id=?",
                                 (rev, uscope, rid_))
                elif summary:
                    conn.execute("UPDATE unresolved SET status='closed', rev=?, scope=? WHERE summary=?",
                                 (rev, uscope, summary))
            r.update({"ok": True})
            results.append(r)
        elif typ == "state":
            scope = norm_scope(op.get("scope"))
            txt = str(op.get("text") or op.get("state_text") or "").strip()
            ts = now_ms()
            conn.execute(
                "INSERT INTO working_state(scope,state_text,updated_ts,rev) VALUES(?,?,?,?)"
                " ON CONFLICT(scope) DO UPDATE SET state_text=excluded.state_text,"
                " updated_ts=excluded.updated_ts, rev=excluded.rev",
                (scope, txt, ts, rev))
            r.update({"ok": True, "scope": scope, "state_text": txt})
            results.append(r)
        elif typ == "consolidate":
            scope = norm_scope(op.get("scope"))
            topic = str(op.get("topic") or "").strip()
            text = str(op.get("text") or "").strip()
            cur = str(op.get("current_state") or "").strip()
            if not topic or not text:
                r.update({"ok": False, "error": "consolidate 需要 topic 和 text"})
                results.append(r)
                continue
            olds = conn.execute(
                "SELECT * FROM memories WHERE status='active' AND scope=? AND topic=?",
                (scope, topic)).fetchall()
            new_id = rid()
            ts = now_ms()
            conn.execute(
                "INSERT INTO memories(id,scope,kind,topic,text,current_state,evidence,status,"
                "supersedes,superseded_by,weight,hits,retracted_reason,created_ts,updated_ts,rev)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id, scope, "fact", topic, text, cur, "[]", "active",
                 ",".join(o["id"] for o in olds), "", 1.2, 0, "", ts, ts, rev))
            for o in olds:
                conn.execute(
                    "UPDATE memories SET status='superseded', superseded_by=?, updated_ts=?, rev=? WHERE id=?",
                    (new_id, ts, rev, o["id"]))
            r.update({"ok": True, "id": new_id, "scope": scope, "topic": topic,
                      "merged": len(olds), "superseded": [o["id"] for o in olds]})
            results.append(r)
        elif typ == "thread_close":
            summary = str(op.get("summary") or "").strip()
            if not summary:
                r.update({"ok": False, "error": "thread_close 需要 summary"})
                results.append(r)
                continue
            tid = str(op.get("thread_id") or op.get("id") or "").strip() or rid()
            ts = now_ms()
            tscope = norm_scope(op.get("scope"))
            conn.execute(
                "INSERT INTO threads(id,scope,summary,ts,day,closed,rev) VALUES(?,?,?,?,?,1,?)"
                " ON CONFLICT(id) DO UPDATE SET scope=excluded.scope, summary=excluded.summary,"
                " ts=excluded.ts, day=excluded.day, closed=1, rev=excluded.rev",
                (tid, tscope, summary, ts, day_of(ts), rev))
            r.update({"ok": True, "id": tid})
            results.append(r)
        else:
            r.update({"ok": False, "error": "未知 op: " + typ})
            results.append(r)
    return results


def find_conflict(conn, ops, base_rev):
    """客户端带 base_rev 时，只对“本次要动的记录”做乐观锁检查。
    返回 None 表示可以写；否则返回冲突详情（HTTP 409）。"""
    if not base_rev:
        return None
    cur = get_rev(conn)
    if base_rev >= cur:
        return None
    for op in ops or []:
        if not isinstance(op, dict):
            continue
        typ = str(op.get("type") or op.get("op") or "").strip()
        scope = norm_scope(op.get("scope"))
        topic = str(op.get("topic") or "").strip()
        rid_ = str(op.get("id") or op.get("recordId") or op.get("supersedes") or "").strip()
        if typ == "state":
            row = conn.execute("SELECT scope FROM working_state WHERE rev>? AND scope=?",
                               (base_rev, scope)).fetchone()
            if row:
                return {"scope": scope, "topic": "__state__", "rev": cur}
        elif typ in ("remember", "append", "consolidate") and topic:
            row = conn.execute(
                "SELECT id FROM memories WHERE rev>? AND scope=? AND topic=? LIMIT 1",
                (base_rev, scope, topic)).fetchone()
            if row:
                return {"scope": scope, "topic": topic, "id": row["id"], "rev": cur}
        elif typ in ("supersede", "correct") and rid_:
            row = conn.execute("SELECT id FROM memories WHERE rev>? AND id=?",
                               (base_rev, rid_)).fetchone()
            if row:
                return {"scope": scope, "id": rid_, "rev": cur}
        elif typ in ("forget", "cleanup", "resolve"):
            row = None
            if topic:
                row = conn.execute(
                    "SELECT id FROM memories WHERE rev>? AND scope=? AND topic=? LIMIT 1",
                    (base_rev, scope, topic)).fetchone()
            if row is None and rid_:
                row = conn.execute("SELECT id FROM memories WHERE rev>? AND id=?",
                                   (base_rev, rid_)).fetchone()
            if row is None:
                about = str(op.get("about") or "").strip()
                if about:
                    row = conn.execute(
                        "SELECT id FROM memories WHERE rev>? AND scope=? AND text LIKE ? LIMIT 1",
                        (base_rev, scope, "%" + about + "%")).fetchone()
            if row:
                return {"scope": scope, "id": row["id"], "rev": cur}
    return None


def do_op(payload):
    op_id = str(payload.get("op_id") or "").strip() or ("srv-" + rid())
    device_id = str(payload.get("device_id") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()
    try:
        base_rev = int(payload.get("base_rev") or 0)
    except (TypeError, ValueError):
        base_rev = 0
    ops = payload.get("ops") or []
    if not isinstance(ops, list):
        ops = []
    conn = connect()
    try:
        with WRITE_LOCK:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT result FROM ops WHERE op_id=?", (op_id,)).fetchone()
            if old is not None:
                conn.execute("ROLLBACK")
                try:
                    res = json.loads(old["result"])
                except Exception:
                    res = {"ok": True}
                res["replayed"] = True
                return res
            conflict = find_conflict(conn, ops, base_rev)
            if conflict is not None:
                conn.execute("ROLLBACK")
                return {"ok": False, "conflict": True, "error": "conflict",
                        "rev": conflict.get("rev"), "base_rev": base_rev,
                        "scope": conflict.get("scope", ""),
                        "topic": conflict.get("topic", ""),
                        "record_id": conflict.get("id", "")}
            rev = bump_rev(conn)
            results = apply_ops(conn, ops, rev, device_id, session_id)
            # 返回这一批 rev 里所有被改动的行（包括被取代/被撤回的旧记录），
            # 否则客户端只拿到新记录，旧记录要等下一次全量同步才会变成 superseded。
            records = [row_mem(r) for r in conn.execute("SELECT * FROM memories WHERE rev=?", (rev,))]
            unresolved = [row_unres(r) for r in conn.execute("SELECT * FROM unresolved WHERE rev=?", (rev,))]
            threads = [row_thread(r) for r in conn.execute("SELECT * FROM threads WHERE rev=?", (rev,))]
            states = [row_working(r) for r in conn.execute("SELECT * FROM working_state WHERE rev=?", (rev,))]
            out = {"ok": True, "rev": rev, "results": results,
                   "records": records, "unresolved": unresolved, "threads": threads,
                   "states": states,
                   "base_rev": base_rev,
                   "behind": max(0, rev - base_rev - 1) if base_rev else 0}
            conn.execute(
                "INSERT INTO ops(op_id,device_id,session_id,base_rev,payload,result,created_ts)"
                " VALUES(?,?,?,?,?,?,?)",
                (op_id, device_id, session_id, base_rev,
                 json.dumps(payload, ensure_ascii=False), json.dumps(out, ensure_ascii=False), now_ms()))
            conn.execute("COMMIT")
            return out
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


# ---------------- 旧接口适配 ----------------

def legacy_append(payload, supersede_id=None):
    op = {"type": "remember", "text": payload.get("text"),
          "current_state": payload.get("current_state"),
          "evidence": payload.get("indexEvidence") or payload.get("evidence"),
          "kind": payload.get("kind") or "fact", "topic": payload.get("topic") or "",
          "scope": payload.get("scope") or ""}
    if supersede_id:
        op["supersedes"] = supersede_id
    r = do_op({"op_id": "legacy-" + rid(), "ops": [op]})
    res = r["results"][0] if r.get("results") else {}
    if not res.get("ok"):
        raise ValueError(res.get("error") or "写入失败")
    rec = {"id": res.get("id")}
    for _r in (r.get("records") or []):
        if _r.get("id") == res.get("id"):
            rec = _r
            break
    unresolved = apply_unresolved(payload.get("unresolvedOps") or [], payload.get("scope") or None)
    return {"ok": True, "record": rec, "dedup": bool(res.get("dedup")),
            "superseded": res.get("superseded", ""), "unresolved": unresolved}


def apply_unresolved(ops, scope=None):
    if not ops:
        return []
    clean = []
    for op in ops:
        if isinstance(op, dict):
            op = dict(op)
            op["type"] = "unresolved"
            if scope and not op.get("scope"):
                op["scope"] = scope
            clean.append(op)
    if not clean:
        return []
    do_op({"op_id": "legacy-unres-" + rid(), "ops": clean})
    conn = connect()
    try:
        if scope:
            rows = conn.execute("SELECT * FROM unresolved WHERE scope=? ORDER BY ts ASC",
                                (norm_scope(scope),)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM unresolved ORDER BY ts ASC").fetchall()
        return [row_unres(r) for r in rows]
    finally:
        conn.close()


def legacy_correct(record_id, reason):
    r = do_op({"op_id": "legacy-" + rid(),
               "ops": [{"type": "correct", "id": record_id, "reason": reason}]})
    res = r.get("results", [{}])[0]
    if not res.get("ok"):
        raise ValueError(res.get("error") or "找不到这条记录")
    return {"ok": True, "record": {"id": record_id, "status": "retracted"},
            "retractedReason": reason}


def legacy_cleanup(ids):
    r = do_op({"op_id": "legacy-" + rid(),
               "ops": [{"type": "cleanup", "ids": ids or []}]})
    res = r.get("results", [{}])[0]
    return {"ok": True, "removed": res.get("removed", 0)}


def legacy_thread_close(summary, scope=None):
    r = do_op({"op_id": "legacy-" + rid(),
               "ops": [{"type": "thread_close", "summary": summary, "scope": scope or "main"}]})
    tid = r.get("results", [{}])[0].get("id")
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM threads WHERE id=?", (tid,)).fetchone()
        return {"ok": True, "thread": row_thread(row) if row else {"summary": summary}}
    finally:
        conn.close()


def session_start(scope=None):
    sc = norm_scope(scope or "main")
    conn = connect()
    try:
        live = conn.execute(
            "SELECT * FROM memories WHERE status='active' AND scope=? ORDER BY created_ts DESC LIMIT 5",
            (sc,)).fetchall()
        unres = conn.execute(
            "SELECT * FROM unresolved WHERE status<>'closed' AND scope=? ORDER BY ts ASC", (sc,)).fetchall()
        th = conn.execute("SELECT * FROM threads WHERE scope=? ORDER BY ts DESC LIMIT 1", (sc,)).fetchone()
        st = conn.execute("SELECT * FROM working_state WHERE scope=?",
                          (sc,)).fetchone()
        return {"ok": True, "persona": q_persona(conn),
                "unresolved": [row_unres(r) for r in unres],
                "thread": row_thread(th) if th else None,
                "state": row_working(st) if st else None,
                "recent": [row_mem(r) for r in live]}
    finally:
        conn.close()


def search_legacy(query, top_n=5, scope=None):
    try:
        top_n = int(top_n)
    except (TypeError, ValueError):
        top_n = 5
    top_n = max(1, min(50, top_n))
    conn = connect()
    try:
        items, msg = rank_records(conn, query or "", top_n, scope)
        return {"ok": True, "items": items, "message": msg}
    finally:
        conn.close()


# ---------------- 迁移 ----------------

def migrate_legacy():
    """旧 JSON 数据 → SQLite（仅在库为空时执行一次）。"""
    conn = connect()
    try:
        n = conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"]
        if n:
            return
        src = LEGACY_DIR
        if not src.exists():
            return
        recs = []
        try:
            recs = json.loads((src / "records.json").read_text(encoding="utf-8"))
        except Exception:
            recs = []
        if not recs:
            return
        with WRITE_LOCK:
            conn.execute("BEGIN IMMEDIATE")
            rev = get_rev(conn) + 1
            for r in recs:
                rid_ = str(r.get("id") or rid())
                ts = int(r.get("ts") or now_ms())
                conn.execute(
                    "INSERT OR IGNORE INTO memories(id,kind,topic,text,current_state,evidence,status,"
                    "supersedes,superseded_by,weight,hits,retracted_reason,created_ts,updated_ts,rev)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rid_, str(r.get("kind") or "fact"), str(r.get("topic") or ""),
                     str(r.get("text") or ""), str(r.get("current_state") or ""),
                     json.dumps(r.get("evidence") or [], ensure_ascii=False),
                     str(r.get("status") or "active"), str(r.get("supersedes") or ""),
                     str(r.get("supersededBy") or ""), float(r.get("weight") or 1),
                     int(r.get("hits") or 0), str(r.get("retractedReason") or ""),
                     ts, ts, rev))
            try:
                unres = json.loads((src / "unresolved.json").read_text(encoding="utf-8"))
            except Exception:
                unres = []
            for u in unres:
                conn.execute("INSERT OR IGNORE INTO unresolved(id,summary,status,ts,rev) VALUES(?,?,?,?,?)",
                             (str(u.get("id") or rid()), str(u.get("summary") or ""),
                              str(u.get("status") or "open"), int(u.get("ts") or now_ms()), rev))
            try:
                ths = json.loads((src / "threads.json").read_text(encoding="utf-8"))
            except Exception:
                ths = []
            for t in ths:
                ts = int(t.get("ts") or now_ms())
                conn.execute("INSERT OR IGNORE INTO threads(id,summary,ts,day,closed,rev) VALUES(?,?,?,?,0,?)",
                             (str(t.get("id") or rid()), str(t.get("summary") or ""), ts,
                              str(t.get("day") or day_of(ts)), rev))
            pf = src / "persona.md"
            if pf.exists():
                conn.execute("INSERT INTO meta(k,v) VALUES('persona',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                             (pf.read_text(encoding="utf-8"),))
            conn.execute("UPDATE meta SET v=? WHERE k='rev'", (str(rev),))
            conn.execute("COMMIT")
            print("migrated %d legacy records from %s" % (len(recs), src))
    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        print("legacy migration skipped: %s" % e, file=sys.stderr)
    finally:
        conn.close()


# ---------------- MCP ----------------

TOOLS = [
    {"name": "latent_session_start", "description": "新会话开场：返回未解决、上次会话线索、最近记忆。",
     "inputSchema": {"type": "object", "properties": {"scope": {"type": "string"}}}},
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
    {"name": "latent_unresolved", "description": "维护未解决清单（可按 scope 隔离）。",
     "inputSchema": {"type": "object", "properties": {"ops": {"type": "array", "items": {"type": "object"}},
                     "scope": {"type": "string", "description": "主线留空或 main；剧场传 stage:<id>"}}, "required": ["ops"]}},
    {"name": "latent_thread_close", "description": "窗口结束时留下会话快照（可按 scope 隔离）。",
     "inputSchema": {"type": "object", "properties": {"summary": {"type": "string"},
                     "scope": {"type": "string"}}, "required": ["summary"]}},
    {"name": "latent_remember", "description": "结构化记忆写入（与网页端同一个真相源）。kind=fact/event/promise/clue/pref，同 scope+topic 则自动取代旧记录。",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string"}, "kind": {"type": "string"}, "topic": {"type": "string"},
         "scope": {"type": "string", "description": "主线留空或 main；剧场传 stage:<id>"},
         "current_state": {"type": "string"}}, "required": ["text"]}},
    {"name": "latent_recall", "description": "服务端统一检索（BM25+时间衰减+权重），可按 scope 隔离。",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "topN": {"type": "integer"}, "scope": {"type": "string"}}, "required": ["query"]}},
    {"name": "latent_state", "description": "读写某个场景的当前状态（working memory，覆盖写，不进入事实链）。带 text 写入，不带 text 读取。",
     "inputSchema": {"type": "object", "properties": {"scope": {"type": "string"}, "text": {"type": "string"}}}},
    {"name": "latent_consolidate", "description": "把同 scope+topic 的多条 active 事实合并成一条，旧记录标 superseded。",
     "inputSchema": {"type": "object", "properties": {
         "scope": {"type": "string"}, "topic": {"type": "string"},
         "text": {"type": "string"}, "current_state": {"type": "string"}}, "required": ["topic", "text"]}},
    {"name": "latent_resolve", "description": "关闭一条未解决事项（可按 scope 隔离）。",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}, "topic": {"type": "string"},
                     "summary": {"type": "string"}, "scope": {"type": "string"}}}},
    {"name": "latent_forget", "description": "遗忘：按 id/topic/about 标记删除。",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}, "topic": {"type": "string"}, "about": {"type": "string"}}}},
    {"name": "latent_snapshot", "description": "增量拉取记忆库（since_rev 之后的变化）。",
     "inputSchema": {"type": "object", "properties": {"since_rev": {"type": "integer"}}}},
]
TOOL_NAMES = {t["name"] for t in TOOLS}


def text_result(text):
    return {"content": [{"type": "text", "text": text}], "isError": False}


def call_tool(name, args):
    if name == "latent_session_start":
        s = session_start(args.get("scope"))
        parts = []
        if s["unresolved"]:
            parts.append("【当前未解决】\n" + "\n".join("- " + u["summary"] for u in s["unresolved"]))
        if s["thread"]:
            parts.append("【上次会话】\n" + s["thread"]["summary"])
        if s["recent"]:
            parts.append("【最近记忆】\n" + "\n".join("- (" + r["day"] + ") " + r["text"][:120] for r in s["recent"]))
        return text_result("\n\n".join(parts) or "记忆库还是空的。")
    if name in ("latent_search", "latent_recall"):
        s = search_legacy(args.get("query", ""), int(args.get("topN") or 5), args.get("scope"))
        if not s["items"]:
            return {"content": [{"type": "text", "text": "没有可靠命中。" + s["message"]}], "isError": True}
        return text_result("\n\n".join("- (" + r["day"] + ") " + r["text"] +
                                       ("\n  现状：" + r["current_state"] if r["current_state"] else "")
                                       for r in s["items"]))
    if name == "latent_append":
        r = legacy_append(args)
        return text_result("已写入 recordId=" + r["record"]["id"])
    if name == "latent_remember":
        r = legacy_append(args)
        return text_result("已写入 recordId=" + r["record"]["id"] +
                           ("（取代旧记录）" if r.get("superseded") else ""))
    if name == "latent_supersede":
        r = legacy_append(args, supersede_id=args.get("supersedes"))
        return text_result("已取代 recordId=" + r["record"]["id"] + " (旧 " + r["superseded"] + ")")
    if name == "latent_correct":
        r = legacy_correct(args.get("recordId"), args.get("reason"))
        return text_result("已撤回 recordId=" + r["record"]["id"])
    if name == "latent_cleanup":
        r = legacy_cleanup(args.get("recordIds"))
        return text_result("已删除 " + str(r["removed"]) + " 条")
    if name == "latent_unresolved":
        changed = apply_unresolved(args.get("ops"), args.get("scope"))
        return text_result("未解决清单已更新，当前 " + str(len(changed)) + " 项")
    if name == "latent_thread_close":
        legacy_thread_close(args.get("summary"), args.get("scope"))
        return text_result("已留下会话线索。")
    if name == "latent_resolve":
        r = do_op({"op_id": "mcp-" + rid(), "ops": [dict(args, type="resolve")]})
        if r.get("conflict"):
            return {"content": [{"type": "text", "text": "冲突：记录已被其他设备更新，请先 latent_snapshot 再重试。"}], "isError": True}
        return text_result("已关闭 " + str(r["results"][0].get("closed", 0)) + " 项")
    if name == "latent_forget":
        r = do_op({"op_id": "mcp-" + rid(), "ops": [dict(args, type="forget")]})
        if r.get("conflict"):
            return {"content": [{"type": "text", "text": "冲突：记录已被其他设备更新，请先 latent_snapshot 再重试。"}], "isError": True}
        return text_result("已遗忘 " + str(r["results"][0].get("removed", 0)) + " 条")
    if name == "latent_snapshot":
        conn = connect()
        try:
            s = q_snapshot(conn, int(args.get("since_rev") or 0), scope=args.get("scope") or None)
            return text_result(json.dumps(s, ensure_ascii=False)[:20000])
        finally:
            conn.close()
    if name == "latent_state":
        sc = norm_scope(args.get("scope") or "main")
        if args.get("text") is not None:
            r = do_op({"op_id": "mcp-" + rid(), "ops": [
                {"type": "state", "scope": sc, "text": str(args.get("text") or "")}]})
            if r.get("conflict"):
                return {"content": [{"type": "text", "text": "冲突：状态已被其他设备更新，请先 latent_snapshot 再重试。"}], "isError": True}
            return text_result("已更新 scope=" + sc + " 的状态。")
        conn = connect()
        try:
            row = conn.execute("SELECT * FROM working_state WHERE scope=?", (sc,)).fetchone()
            return text_result(row["state_text"] if row else "(这个场景还没有状态记录)")
        finally:
            conn.close()
    if name == "latent_consolidate":
        r = do_op({"op_id": "mcp-" + rid(), "ops": [{
            "type": "consolidate", "scope": args.get("scope") or "main",
            "topic": args.get("topic") or "", "text": args.get("text") or "",
            "current_state": args.get("current_state") or ""}]})
        if r.get("conflict"):
            return {"content": [{"type": "text", "text": "冲突：这组事实已被其他设备更新，请先 latent_snapshot 再重试。"}], "isError": True}
        res = r["results"][0]
        return text_result("已合并 " + str(res.get("merged", 0)) + " 条为 recordId=" + str(res.get("id")))
    return {"content": [{"type": "text", "text": "未知工具 " + name}], "isError": True}


# ---------------- HTTP ----------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LatentMemory/2.0"

    def log_message(self, fmt, *args):
        pass

    def cors(self):
        origin = (self.headers.get("Origin") or "").strip()
        allowed = ""
        if origin in ("null", "capacitor://localhost", "ionic://localhost"):
            allowed = origin
        elif origin:
            try:
                u = urlparse(origin)
                host = (u.hostname or "").lower()
                if u.scheme in ("http", "https") and (
                    host in ("localhost", "127.0.0.1", "::1")
                    or host.startswith("192.168.")
                    or host.startswith("10.")
                    or any(host.startswith("172.%d." % n) for n in range(16, 32))
                    or host.endswith(".local")
                ):
                    allowed = origin
            except Exception:
                allowed = ""
        if allowed:
            self.send_header("Access-Control-Allow-Origin", allowed)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Accept, Mcp-Session-Id, Authorization, X-Latent-Token")
        self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id")

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if code >= 400:
            # 错误响应前可能没有读完请求体；关闭连接，避免同一 keep-alive 连接上的下一个请求错位
            self.send_header("Connection", "close")
            self.close_connection = True
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if n < 0 or n > MAX_BODY:
            self.close_connection = True
            return None
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return None

    def token_from_cookie(self):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "latent_token":
                return v.strip()
        return ""

    def authed(self):
        if not TOKEN:
            return True
        h = (self.headers.get("Authorization") or "").strip()
        tok = ""
        if h.lower().startswith("bearer "):
            tok = h[7:].strip()
        if not tok:
            tok = (self.headers.get("X-Latent-Token") or "").strip()
        if not tok:
            # 配对链接打开过一次后，同源请求靠 cookie 就能过；书签也能直接用
            tok = self.token_from_cookie()
        # 常量时间比较，避免用响应时间逐字符猜 token
        return bool(tok) and hmac.compare_digest(tok, TOKEN)

    def require_auth(self):
        if self.authed():
            return True
        self.send_json({"ok": False, "error": "unauthorized",
                        "hint": "需要 token；打开配对链接 http://<host>:<port>/#t=<token>，"
                                "或在设置里粘贴 ~/latent-memory/token 的内容"}, 401)
        return False

    def proxy_authed(self):
        if not TOKEN:
            return True
        tok = (self.headers.get("X-Latent-Token") or "").strip() or self.token_from_cookie()
        return bool(tok) and hmac.compare_digest(tok, TOKEN)

    def serve_static(self, p):
        if STATIC_DIR is None:
            return False
        rel = (p or "/").lstrip("/") or "index.html"
        try:
            base = STATIC_DIR.resolve()
            fp = (base / rel).resolve()
            if os.path.commonpath([str(fp), str(base)]) != str(base):
                return False
            if fp.is_dir():
                fp = fp / "index.html"
            parts = [x for x in Path(rel).parts]
            if any(x in STATIC_DENY_NAME or x.startswith(".") or x.startswith("backup-")
                   for x in parts):
                return False
            if (fp.suffix.lower() not in STATIC_EXTS
                    or fp.suffix.lower() in STATIC_DENY_EXT or not fp.is_file()):
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
        # 一律校验 X-Latent-Token：Android 的 127.0.0.1 是所有 App 共享的，
        # "本机"不等于"页面自己"；这里不读 Authorization，那是上游模型的 Key。
        if not self.proxy_authed():
            self.send_json({"ok": False, "error": "unauthorized",
                            "hint": "带 X-Latent-Token；token 在数据目录的 token 文件里"}, 401)
            return True
        if p == "/api/proxy":
            sub = (parse_qs(q).get("p") or ["/"])[0]
            if not sub.startswith("/") or ".." in sub:
                self.send_json({"ok": False, "error": "bad proxy path"}, 400)
                return True
            url = PROXY_BASE.rstrip("/") + sub
        else:
            url = PROXY_BASE.rstrip("/") + p[len("/api"):]
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
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
            self.send_json({"error": str(e)}, 502)
            return True
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
        qs = parse_qs(u.query)
        # 配对链接 /?t=<token>：种一个同源 cookie 再跳回干净地址。
        # 这样以后直接打开书签、或刷新页面都不用再贴 token。
        if qs.get("t"):
            cand = (qs.get("t") or [""])[0]
            if TOKEN and cand and hmac.compare_digest(cand, TOKEN):
                self.send_response(302)
                self.send_header("Set-Cookie",
                                 "latent_token=%s; Path=/; Max-Age=31536000; SameSite=Lax; HttpOnly" % TOKEN)
                # 跳到带 #t= 的干净地址：hash 不发给服务端，但页面脚本能读到它并把 token
                # 存进 localStorage。只靠 cookie 的话，脚本永远不知道 token，换浏览器/
                # 清缓存后就会变成"已连接但备份 401"。
                self.send_header("Location", (p or "/") + "#t=" + TOKEN)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        if p == "/health":
            conn = connect()
            try:
                n = conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"]
            finally:
                conn.close()
            return self.send_json({"ok": True, "service": "latent-memory",
                                   "records": n, "rev": self._rev()})
        if (p.startswith("/memory/") or p in ("/state", "/session_start", "/persona")) and not self.require_auth():
            return
        if p == "/memory/backups":
            return self.send_json({"ok": True, "dir": str(BACKUP_DIR or ""),
                                   "keep": BACKUP_KEEP, "interval": BACKUP_EVERY,
                                   "items": backup_list()})
        if p == "/memory/backup":
            name = (qs.get("name") or [""])[0]
            if not re.match(r"^latent-\d{8}-\d{6}\.db$", name):
                return self.send_json({"ok": False, "error": "bad backup name"}, 400)
            fp = (BACKUP_DIR / name) if BACKUP_DIR else None
            if not fp or not fp.is_file():
                return self.send_json({"ok": False, "error": "not found"}, 404)
            data = fp.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", "attachment; filename=\"%s\"" % name)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.cors()
            self.end_headers()
            self.wfile.write(data)
            return
        if p == "/memory/snapshot":
            try:
                since = int((qs.get("since_rev") or ["0"])[0] or 0)
            except (TypeError, ValueError):
                return self.send_json({"ok": False, "error": "bad since_rev"}, 400)
            conn = connect()
            try:
                scope = (qs.get("scope") or [""])[0] or None
                return self.send_json(q_snapshot(conn, since, scope=scope))
            finally:
                conn.close()
        if p == "/memory/recall":
            query = (qs.get("q") or [""])[0]
            try:
                k = int((qs.get("k") or qs.get("topN") or ["6"])[0])
            except ValueError:
                k = 6
            scope = (qs.get("scope") or [""])[0] or None
            conn = connect()
            try:
                items, msg = rank_records(conn, query, max(1, min(50, k)), scope)
                return self.send_json({"ok": True, "rev": get_rev(conn),
                                       "items": items, "message": msg})
            finally:
                conn.close()
        if p == "/memory/state":
            conn = connect()
            try:
                sc = norm_scope((qs.get("scope") or ["main"])[0])
                row = conn.execute("SELECT * FROM working_state WHERE scope=?", (sc,)).fetchone()
                return self.send_json({"ok": True, "scope": sc,
                                       "state": row_working(row) if row else None,
                                       "rev": get_rev(conn)})
            finally:
                conn.close()
        if p == "/memory/stream":
            try:
                since = int((qs.get("since_rev") or ["0"])[0] or 0)
            except (TypeError, ValueError):
                return self.send_json({"ok": False, "error": "bad since_rev"}, 400)
            return self.stream_sse(since)
        if p == "/memory/export":
            conn = connect()
            try:
                return self.send_json(q_state(conn))
            finally:
                conn.close()
        if p == "/state":
            conn = connect()
            try:
                return self.send_json(q_state(conn))
            finally:
                conn.close()
        if p == "/session_start":
            return self.send_json(session_start((qs.get("scope") or [""])[0] or None))
        if p == "/persona":
            conn = connect()
            try:
                return self.send_json({"ok": True, "persona": q_persona(conn)})
            finally:
                conn.close()
        if self.serve_static(p):
            return
        return self.send_json({"ok": False, "error": "not found"}, 404)

    def _rev(self):
        conn = connect()
        try:
            return get_rev(conn)
        finally:
            conn.close()

    def stream_sse(self, since_rev):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.cors()
        self.end_headers()
        self.close_connection = True
        last = since_rev
        start = time.time()
        try:
            while time.time() - start < 300:
                conn = connect()
                try:
                    rev = get_rev(conn)
                    if rev > last:
                        snap = q_snapshot(conn, last)
                        last = rev
                    else:
                        snap = None
                finally:
                    conn.close()
                if snap is not None:
                    self.wfile.write(("data: " + json.dumps(snap, ensure_ascii=False) + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                else:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                time.sleep(2)
        except Exception:
            pass
        return

    def do_PUT(self):
        u = urlparse(self.path)
        if u.path == "/persona":
            if not self.require_auth():
                return
            body = self.read_json()
            if body is None:
                return self.send_json({"ok": False, "error": "bad json"}, 400)
            if not isinstance(body, dict):
                return self.send_json({"ok": False, "error": "body must be a JSON object"}, 400)
            conn = connect()
            try:
                with WRITE_LOCK:
                    conn.execute("BEGIN IMMEDIATE")
                    rev = bump_rev(conn)
                    conn.execute("INSERT INTO meta(k,v) VALUES('persona',?) "
                                 "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                                 (str(body.get("persona") or ""),))
                    conn.execute("INSERT INTO meta(k,v) VALUES('persona_rev',?) "
                                 "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(rev),))
                    conn.execute("COMMIT")
            finally:
                conn.close()
            return self.send_json({"ok": True, "rev": self._rev()})
        return self.send_json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        if p.startswith("/api/"):
            if self.proxy_api(p, u.query):
                return
        protected = (p.startswith("/memory/") or
                     p in ("/append", "/supersede", "/correct", "/cleanup", "/search",
                           "/unresolved", "/thread_close", "/session_start", "/mcp", "/persona"))
        if protected and not self.require_auth():
            return
        body = self.read_json()
        if body is None:
            return self.send_json({"ok": False, "error": "bad json or body too large"}, 400)
        if not isinstance(body, dict):
            return self.send_json({"ok": False, "error": "body must be a JSON object"}, 400)
        try:
            if p == "/memory/backup":
                try:
                    b = make_backup("manual")
                except Exception as e:
                    return self.send_json({"ok": False, "error": str(e)}, 500)
                return self.send_json({"ok": True, "created": b, "items": backup_list()})
            if p == "/mcp":
                return self.handle_mcp(body)
            if p == "/memory/op":
                r = do_op(body)
                return self.send_json(r, 409 if r.get("conflict") else 200)
            if p == "/memory/state":
                r = do_op({"op_id": body.get("op_id") or ("srv-" + rid()),
                           "device_id": body.get("device_id") or "",
                           "session_id": body.get("session_id") or "",
                           "base_rev": int(body.get("base_rev") or 0),
                           "ops": [{"type": "state", "scope": body.get("scope"),
                                    "text": body.get("text") or body.get("state_text") or ""}]})
                return self.send_json(r, 409 if r.get("conflict") else 200)
            if p == "/memory/recall":
                try:
                    k = int(body.get("k") or body.get("topN") or 6)
                except (TypeError, ValueError):
                    k = 6
                conn = connect()
                try:
                    items, msg = rank_records(conn, body.get("query", ""),
                                              max(1, min(50, k)), body.get("scope") or None)
                    return self.send_json({"ok": True, "rev": get_rev(conn),
                                           "items": items, "message": msg})
                finally:
                    conn.close()
            if p == "/persona":
                rev = set_persona(body.get("persona"))
                return self.send_json({"ok": True, "rev": rev,
                                       "persona": str(body.get("persona") or "")})
            if p == "/append":
                return self.send_json(legacy_append(body))
            if p == "/supersede":
                return self.send_json(legacy_append(body, supersede_id=body.get("supersedes") or body.get("recordId")))
            if p == "/correct":
                return self.send_json(legacy_correct(body.get("recordId"), body.get("reason")))
            if p == "/cleanup":
                return self.send_json(legacy_cleanup(body.get("recordIds")))
            if p == "/search":
                return self.send_json(search_legacy(body.get("query", ""), int(body.get("topN") or 5)))
            if p == "/unresolved":
                return self.send_json({"ok": True,
                                       "changed": apply_unresolved(body.get("ops"), body.get("scope") or None)})
            if p == "/thread_close":
                return self.send_json(legacy_thread_close(body.get("summary"), body.get("scope")))
            if p == "/session_start":
                return self.send_json(session_start(body.get("scope")))
            return self.send_json({"ok": False, "error": "not found"}, 404)
        except ValueError as e:
            return self.send_json({"ok": False, "error": str(e)}, 400)
        except Exception as e:
            return self.send_json({"ok": False, "error": type(e).__name__ + ": " + str(e)}, 500)

    def handle_mcp(self, body):
        if not self.authed():
            return self.send_json({"jsonrpc": "2.0", "id": body.get("id"),
                                   "error": {"code": -32001, "message": "unauthorized"}}, 401)
        method = body.get("method")
        mid = body.get("id")
        if isinstance(method, str) and method.startswith("notifications/") and mid is None:
            self.send_response(202)
            self.cors()
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "initialize":
            res = {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "latent-memory", "version": "2.0"}}}
            raw = json.dumps(res, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Mcp-Session-Id", uuid.uuid4().hex)
            self.send_header("Content-Length", str(len(raw)))
            self.cors()
            self.end_headers()
            self.wfile.write(raw)
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
                                       {"content": [{"type": "text", "text": "错误：" + str(e)}],
                                        "isError": True}})
        if method == "ping":
            return self.send_json({"jsonrpc": "2.0", "id": mid, "result": {}})
        return self.send_json({"jsonrpc": "2.0", "id": mid,
                               "error": {"code": -32601, "message": "未实现 " + str(method)}})


def main():
    global STATIC_DIR, PROXY_BASE, TOKEN, DB_PATH, LEGACY_DIR, BACKUP_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--data", default=str(Path.home() / "latent-memory"))
    ap.add_argument("--static", default="", help="静态文件目录（给 Daddy 页面用）")
    ap.add_argument("--proxy", default="", help="把 /api/* 转发到这个上游")
    ap.add_argument("--lan", action="store_true", help="监听 0.0.0.0（无鉴权时仅限可信局域网）")
    ap.add_argument("--token", default=os.environ.get("LATENT_TOKEN", ""),
                    help="Bearer 鉴权 token；不填会自动生成并写入数据目录")
    ap.add_argument("--no-token", action="store_true",
                    help="明确关闭鉴权（只建议本机调试；Android 上任何 App 都能访问本机端口）")
    ap.add_argument("--no-backup", action="store_true", help="关闭自动备份")
    a = ap.parse_args()
    LEGACY_DIR = Path(a.data)
    DB_PATH = Path(a.data) / "latent.db"
    TOKEN = (a.token or "").strip()
    token_new = False
    if not TOKEN and not a.no_token:
        TOKEN, token_new = read_or_create_token()
    if a.static:
        STATIC_DIR = Path(a.static).resolve()
    else:
        # 不带 --static 直接运行时，默认把脚本所在目录当站点根（index.html 在那儿就成立），
        # 否则手敲 `python3 latent-server.py --port 8765` 会得到一个连首页都 404 的服务。
        cand = Path(__file__).resolve().parent
        if (cand / "index.html").is_file():
            STATIC_DIR = cand
    PROXY_BASE = a.proxy
    if a.lan:
        a.host = "0.0.0.0"
    init_db()
    migrate_legacy()
    if not a.no_backup:
        BACKUP_DIR = Path(a.data) / "backups"
        threading.Thread(target=backup_loop, daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    print("Latent memory server v2 on http://%s:%d  (data: %s, auth: %s)"
          % (a.host, a.port, DB_PATH.parent, "on" if TOKEN else "off"))
    if TOKEN:
        shown = "127.0.0.1" if a.host in ("0.0.0.0", "::") else a.host
        if token_new:
            print("已生成访问 token：%s" % TOKEN)
        else:
            print("访问 token：%s" % TOKEN)
        print("token 文件：%s" % token_path())
        print("配对链接（手机上打开一次，页面和书签都会记住）：http://%s:%d/?t=%s"
              % (shown, a.port, TOKEN))
    else:
        print("WARNING: 鉴权已关闭；本机任何 App 都能读写记忆、盗用模型代理。", file=sys.stderr)
    if a.host not in ("127.0.0.1", "localhost", "::1") and not TOKEN:
        print("WARNING: listening on %s without token; anyone on this network can read/write memory." % a.host,
              file=sys.stderr)
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
