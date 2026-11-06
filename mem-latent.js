/* mem-latent.js — 共享记忆客户端（v2）
 * 真相源在服务端（SQLite）；本地只做三件事：
 *   1. IndexedDB 缓存，离线可读；
 *   2. 未同步操作队列（op_id 幂等，失败不丢，退避重试）；
 *   3. 按 rev 增量同步 + 服务端统一检索。
 * 对外接口保持旧名，index.html 不需要大改。
 */
(function () {
  'use strict';

  var DB_NAME = 'caeruleus-latent.v1';
  var DB_VER = 2;
  var BASE_KEY = 'daddy.latent.base';
  var DEVICE_KEY = 'daddy.latent.device';
  var LAST_REV_KEY = 'daddy.latent.rev';

  var db = null;
  var base = '';
  var mode = 'local';
  var state = { records: [], unresolved: [], threads: [], states: {}, persona: '', rev: 0 };
  var outbox = [];
  var ackedOps = []; /* 已同步成功、待从 IndexedDB 删除的 op_id */
  var offlineUntil = 0; /* 最近一次同步失败后的静默期，避免每次说话都等超时 */
  var tab = 'memory';
  var query = '';
  var searchHits = null;
  var flushing = false;
  var booted = false;
  var syncTimer = null;
  var es = null;
  var lastSyncError = '';

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function nowMs() { return Date.now(); }
  function normScope(v) {
    v = String(v == null ? '' : v).trim();
    if (!v) return 'main';
    return v.replace(/[\x00-\x1f]/g, '').slice(0, 64);
  }
  function uid(p) { return (p || '') + Math.random().toString(16).slice(2, 10) + nowMs().toString(16).slice(-6); }

  function deviceId() {
    var d = '';
    try { d = localStorage.getItem(DEVICE_KEY) || ''; } catch (e) {}
    if (!d) {
      d = 'dev-' + Math.random().toString(16).slice(2, 10);
      try { localStorage.setItem(DEVICE_KEY, d); } catch (e) {}
    }
    return d;
  }
  function sessionId() {
    var k = 'daddy.latent.session';
    var s = '';
    try { s = sessionStorage.getItem(k) || ''; } catch (e) {}
    if (!s) {
      s = 'ses-' + Math.random().toString(16).slice(2, 10);
      try { sessionStorage.setItem(k, s); } catch (e) {}
    }
    return s;
  }

  function openDB() {
    return new Promise(function (resolve, reject) {
      if (db) return resolve(db);
      var req = indexedDB.open(DB_NAME, DB_VER);
      req.onupgradeneeded = function (e) {
        var d = e.target.result;
        if (!d.objectStoreNames.contains('kv')) d.createObjectStore('kv', { keyPath: 'k' });
        if (!d.objectStoreNames.contains('outbox')) d.createObjectStore('outbox', { keyPath: 'op_id' });
      };
      req.onsuccess = function () { db = req.result; resolve(db); };
      req.onerror = function () { reject(req.error); };
      req.onblocked = function () { reject(new Error('IndexedDB blocked')); };
    });
  }
  function dbGet(k) {
    return openDB().then(function (d) {
      return new Promise(function (res) {
        var r = d.transaction('kv', 'readonly').objectStore('kv').get(k);
        r.onsuccess = function () { res(r.result ? r.result.v : null); };
        r.onerror = function () { res(null); };
      });
    });
  }
  function dbSet(k, v) {
    return openDB().then(function (d) {
      return new Promise(function (res) {
        var tx;
        try { tx = d.transaction('kv', 'readwrite'); } catch (e) { res(false); return; }
        try { tx.objectStore('kv').put({ k: k, v: v }); } catch (e) { res(false); return; }
        tx.oncomplete = function () { res(true); };
        tx.onerror = function () { res(false); };
        tx.onabort = function () { res(false); };
      });
    });
  }
  function obAll() {
    return openDB().then(function (d) {
      return new Promise(function (res) {
        var r = d.transaction('outbox', 'readonly').objectStore('outbox').getAll();
        r.onsuccess = function () { res(r.result || []); };
        r.onerror = function () { res([]); };
      });
    });
  }
  function obPut(op) {
    return openDB().then(function (d) {
      return new Promise(function (res) {
        var tx = d.transaction('outbox', 'readwrite');
        tx.objectStore('outbox').put(op);
        tx.oncomplete = function () { res(true); };
        tx.onerror = function () { res(false); };
      });
    });
  }
  function obDel(opId) {
    return openDB().then(function (d) {
      return new Promise(function (res) {
        var tx = d.transaction('outbox', 'readwrite');
        tx.objectStore('outbox').delete(opId);
        tx.oncomplete = function () { res(true); };
        tx.onerror = function () { res(false); };
      });
    });
  }

  function persist() {
    return dbSet('state', state).then(function () {
      try { localStorage.setItem(LAST_REV_KEY, String(state.rev || 0)); } catch (e) {}
    });
  }
  function persistOutbox() {
    /* 只删已经确认同步的 op_id，再写入当前队列。
       不用 clear()：两个窗口同时开着时，另一个窗口会写入新 op，
       清空会把它们一起抹掉；定向删除也能避免刷新后旧 op 复活。 */
    return openDB().then(function (d) {
      return new Promise(function (res) {
        var tx;
        try { tx = d.transaction('outbox', 'readwrite'); } catch (e) { res(false); return; }
        var store = tx.objectStore('outbox');
        try {
          outbox.forEach(function (o) { store.put(o); });
          ackedOps.forEach(function (id) { store.delete(id); });
        } catch (e) { res(false); return; }
        tx.oncomplete = function () { ackedOps = []; res(true); };
        tx.onerror = function () { res(false); };
        tx.onabort = function () { res(false); };
      });
    }).then(function () {
      var st = $('memConnTxt');
      if (st && outbox.length) st.textContent = '待同步 ' + outbox.length;
    });
  }

  function setConn(ok, txt) {
    var d = $('memConnDot'), t = $('memConnTxt');
    if (d) { d.classList.toggle('on', !!ok); d.classList.toggle('off', !ok); }
    if (t) t.textContent = txt || (ok ? (outbox.length ? '已连接 · 待同步 ' + outbox.length : '已连接') : '本地模式');
  }

  function api(path, body, timeout) {
    var url = base.replace(/\/+$/, '') + path;
    var opt = { method: body === undefined || body === null ? 'GET' : 'POST', headers: {} };
    if (opt.method === 'POST') { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    var tok = '';
    try { tok = localStorage.getItem('daddy.latent.token') || ''; } catch (e) {}
    /* 记忆接口只认 X-Latent-Token；Authorization 留给上游模型的 Key，两者不能混用 */
    if (tok) opt.headers['X-Latent-Token'] = tok;
    var ctl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    if (ctl) { opt.signal = ctl.signal; }
    var timer = setTimeout(function () { if (ctl) { try { ctl.abort(); } catch (e) {} } }, timeout || 12000);
    return fetch(url, opt).then(function (r) {
      clearTimeout(timer);
      if (r.status === 401) throw new Error('unauthorized');
      return r.text().then(function (t) {
        var d = null;
        try { d = JSON.parse(t); } catch (e) {}
        if (!r.ok) {
          var err = new Error((d && (d.error || d.message)) || ('HTTP ' + r.status));
          err.status = r.status;
          err.data = d;
          throw err;
        }
        if (!d) throw new Error('bad json');
        return d;
      });
    }, function (e) { clearTimeout(timer); throw e; });
  }

  function normalizeBase(v) {
    v = String(v || '').trim();
    if (!v) return '';
    if (!/^https?:\/\//i.test(v)) v = 'http://' + v;
    v = v.replace(/\/+$/, '');
    /* 服务端接口挂在根路径；容忍用户把旧的 /api 后缀一起粘进来 */
    v = v.replace(/\/api$/i, '');
    return v;
  }

  function recById(id) {
    for (var i = 0; i < state.records.length; i++) if (state.records[i].id === id) return state.records[i];
    return null;
  }
  function mergeRecords(list, dropIds) {
    var byId = {};
    state.records.forEach(function (r) { byId[r.id] = r; });
    (list || []).forEach(function (r) {
      if (!r || !r.id) return;
      byId[r.id] = r;
    });
    var arr = Object.keys(byId).map(function (k) { return byId[k]; });
    if (dropIds && dropIds.length) {
      var drop = {};
      dropIds.forEach(function (x) { drop[x] = 1; });
      arr = arr.filter(function (r) { return !drop[r.id]; });
    }
    arr.sort(function (a, b) { return (a.ts || 0) - (b.ts || 0); });
    state.records = arr;
  }
  function mergeUnresolved(list) {
    var byId = {};
    state.unresolved.forEach(function (r) { byId[r.id] = r; });
    (list || []).forEach(function (r) { if (r && r.id) byId[r.id] = r; });
    state.unresolved = Object.keys(byId).map(function (k) { return byId[k]; });
  }
  function mergeThreads(list) {
    var byId = {};
    state.threads.forEach(function (r) { byId[r.id] = r; });
    (list || []).forEach(function (r) { if (r && r.id) byId[r.id] = r; });
    state.threads = Object.keys(byId).map(function (k) { return byId[k]; });
  }
  function applySnapshot(snap) {
    if (!snap) return;
    if (snap.records) mergeRecords(snap.records);
    if (snap.unresolved) mergeUnresolved(snap.unresolved);
    if (snap.threads) mergeThreads(snap.threads);
    (snap.states || []).forEach(function (st) {
      if (st && st.scope) state.states[st.scope] = st;
    });
    if (typeof snap.persona === 'string') state.persona = snap.persona;
    if (snap.rev && snap.rev > state.rev) state.rev = snap.rev;
  }

  /* ---------- 本地乐观更新 ---------- */

  function localApply(op) {
    var a = op.args || {};
    var scope = normScope(a.scope);
    if (op.type === 'remember') {
      var topic = String(a.topic || '').trim();
      if (topic) {
        state.records.forEach(function (r) {
          if (normScope(r.scope) === scope && r.topic === topic && r.status === 'active' && r.id !== op.tempId) {
            r.status = 'superseded';
            r.supersededBy = op.tempId;
          }
        });
      }
      state.records.push({
        id: op.tempId, scope: scope, kind: a.kind || 'fact', topic: topic,
        text: a.text, current_state: a.current_state || '',
        ts: nowMs(), day: new Date().toISOString().slice(0, 10),
        status: 'active', weight: 1, supersedes: '', supersededBy: '',
        rev: -1, pending: true
      });
    } else if (op.type === 'supersede') {
      var old = recById(a.id);
      if (old) { old.status = 'superseded'; old.supersededBy = op.tempId; }
      state.records.push({
        id: op.tempId, scope: (old && old.scope) || scope,
        kind: (old && old.kind) || 'fact', topic: (old && old.topic) || '',
        text: a.text, current_state: a.current_state || '',
        ts: nowMs(), day: new Date().toISOString().slice(0, 10),
        status: 'active', weight: 1, supersedes: a.id, supersededBy: '',
        rev: -1, pending: true
      });
    } else if (op.type === 'consolidate') {
      var topic2 = String(a.topic || '').trim();
      var olds = [];
      state.records.forEach(function (r) {
        if (normScope(r.scope) === scope && r.topic === topic2 && r.status === 'active') {
          olds.push(r.id); r.status = 'superseded'; r.supersededBy = op.tempId;
        }
      });
      state.records.push({
        id: op.tempId, scope: scope, kind: 'fact', topic: topic2,
        text: a.text, current_state: a.current_state || '',
        ts: nowMs(), day: new Date().toISOString().slice(0, 10),
        status: 'active', weight: 1.2, supersedes: olds.join(','), supersededBy: '',
        rev: -1, pending: true
      });
    } else if (op.type === 'correct') {
      var r = recById(a.id);
      if (r) { r.status = 'retracted'; r.retractedReason = a.reason || ''; }
    } else if (op.type === 'forget') {
      var ids = [];
      if (a.id) ids.push(a.id);
      if (a.topic) state.records.forEach(function (x) { if (normScope(x.scope) === scope && x.topic === a.topic && x.status === 'active') ids.push(x.id); });
      if (a.about) state.records.forEach(function (x) {
        if (normScope(x.scope) === scope && x.status === 'active' && String(x.text || '').indexOf(a.about) >= 0) ids.push(x.id);
      });
      ids.forEach(function (id) { var rr = recById(id); if (rr) rr.status = 'deleted'; });
    } else if (op.type === 'resolve') {
      var sameScope = function (u) { return (u.scope || 'main') === scope; };
      if (a.id) {
        state.unresolved.forEach(function (u) { if (u.id === a.id && sameScope(u)) u.status = 'closed'; });
        state.records.forEach(function (r2) { if (r2.id === a.id && r2.status === 'active' && (r2.scope || 'main') === scope) r2.status = 'closed'; });
      }
      if (a.summary) state.unresolved.forEach(function (u) { if (u.summary === a.summary && sameScope(u)) u.status = 'closed'; });
      if (a.topic) state.records.forEach(function (r2) { if (normScope(r2.scope) === scope && r2.topic === a.topic && r2.status === 'active') r2.status = 'closed'; });
    } else if (op.type === 'state') {
      var st = state.states[scope] || { scope: scope };
      st.state_text = String(a.text || '');
      st.updated_ts = nowMs();
      st.pending = true;
      state.states[scope] = st;
    } else if (op.type === 'thread_close') {
      state.threads.push({ id: op.tempId, scope: scope, summary: a.summary, ts: nowMs(),
                           closed: true, rev: -1, pending: true });
    }
  }

  function enqueue(type, args) {
    var op = {
      op_id: uid('op-'), type: type, args: args || {},
      tempId: uid(type === 'remember' || type === 'supersede' || type === 'consolidate' || type === 'thread_close' ? 'tmp-' : ''),
      attempts: 0, nextAt: 0, created: nowMs()
    };
    localApply(op);
    outbox.push(op);
    renderAll();
    persist();
    persistOutbox();
    flushSoon(50);
    return op.op_id;
  }

  function ackRecords(res, ackedOp) {
    var drop = [];
    var recs = [];
    (res.records || []).forEach(function (r) {
      if (r && r.id) recs.push(r);
    });
    // 用服务端返回的记录替换对应临时记录
    /* 只清理这一条已发送 op 的临时记录；后面还没发的 op 不能一起清掉 */
    var pendingTemp = ackedOp && ackedOp.tempId ? [ackedOp] : [];
    state.records.forEach(function (r) {
      if (r.pending) {
        var same = pendingTemp.some(function (o) {
          return o.tempId === r.id && (o.type === 'remember' || o.type === 'supersede');
        });
        if (same) drop.push(r.id);
      }
    });
    mergeRecords(recs, drop);
    /* op 的副作用（被取代的旧记录、被关闭的未解决项/线程）也在这一批 rev 里 */
    if (res && res.unresolved) mergeUnresolved(res.unresolved);
    if (res && res.threads) mergeThreads(res.threads);
    (res.states || []).forEach(function (st) {
      if (st && st.scope) state.states[st.scope] = st;
    });
    // 服务端 id 回填到尚未发出的后续操作里
    var results = res.results || [];
    results.forEach(function (rr, i) {
      var o = outbox[i];
      if (!o || !o.tempId || !rr || !rr.id) return;
      outbox.forEach(function (q) {
        ['id', 'supersedes'].forEach(function (f) {
          if (q.args && q.args[f] === o.tempId) q.args[f] = rr.id;
        });
      });
      /* 本地旧记录的变迁链也要从临时 id 改成服务端 id */
      state.records.forEach(function (r) {
        if (r.supersededBy === o.tempId) r.supersededBy = rr.id;
        if (r.supersedes === o.tempId) r.supersedes = rr.id;
      });
      if (o.type === 'thread_close') {
        state.threads.forEach(function (t) {
          if (t.id === o.tempId) { t.id = rr.id; t.pending = false; t.rev = res.rev || t.rev; }
        });
      }
    });
  }

  function flush() {
    if (mode !== 'remote' || flushing || !outbox.length) return Promise.resolve();
    flushing = true;
    var op = outbox[0];
    var payload = {
      op_id: op.op_id, device_id: deviceId(), session_id: sessionId(),
      base_rev: state.rev || 0,
      ops: [Object.assign({ type: op.type }, op.args)]
    };
    return api('/memory/op', payload, 12000).then(function (res) {
      /* 先回执再出队：ackRecords 依赖 outbox[i] 与 results[i] 一一对应 */
      ackRecords(res, outbox[0]);
      ackedOps.push(op.op_id);
      outbox.shift();
      /* 服务端逐条 op 的结果里可能有失败（比如目标记录已不存在）：别静默吞掉 */
      try {
        var bad = (res.results || []).filter(function (r) { return r && r.ok === false; })[0];
        if (bad && window.toast) window.toast('有一条记忆没存上：' + (bad.error || '未知原因'));
      } catch (e) {}
      if (res.rev && res.rev > state.rev) state.rev = res.rev;
      offlineUntil = 0;
      return persist().then(persistOutbox).then(function () {
        flushing = false;
        renderAll();
        setConn(true, outbox.length ? '已连接 · 待同步 ' + outbox.length : '已连接 · rev ' + state.rev);
        if (flushTimer) { clearTimeout(flushTimer); flushTimer = null; }
        if (outbox.length) flushSoon(120);
      });
    }).catch(function (e) {
      if (e && e.status === 409) {
        /* 乐观锁冲突：服务端已有更新的版本。先拉全量，再重放同一条 op（op_id 不变）。 */
        flushing = false;
        lastSyncError = 'conflict';
        pull().then(function () {
          setConn(true, outbox.length ? '已连接 · 待同步 ' + outbox.length : '已连接 · rev ' + state.rev);
          flushSoon(120);
        }, function () {
          op.nextAt = nowMs() + 3000;
          flushSoon(3000);
        });
        return;
      }
      op.attempts = (op.attempts || 0) + 1;
      var wait = Math.min(30000, 800 * Math.pow(1.8, Math.min(8, op.attempts)));
      op.nextAt = nowMs() + wait;
      lastSyncError = e.message || String(e);
      offlineUntil = nowMs() + 15000;
      flushing = false;
      persistOutbox();
      setConn(false, '离线 · 待同步 ' + outbox.length);
      renderAll();
      flushSoon(wait);
    });
  }
  var flushTimer = null;
  function flushSoon(ms) {
    if (flushTimer) return;
    flushTimer = setTimeout(function () { flushTimer = null; flush(); }, ms || 100);
  }

  function pull() {
    if (mode !== 'remote') return Promise.resolve();
    return api('/memory/snapshot?since_rev=' + (state.rev || 0), null, 12000).then(function (snap) {
      applySnapshot(snap);
      if (snap.rev < state.rev) {
        // 服务端被重置或换了库：全量重拉
        state.rev = 0;
        return api('/memory/snapshot?since_rev=0', null, 12000).then(function (full) {
          state.records = [];
          state.unresolved = [];
          state.threads = [];
          state.states = {};
          applySnapshot(full);
        });
      }
    }).then(function () {
      offlineUntil = 0;
      return persist();
    }).then(function () {
      setConn(true, outbox.length ? '已连接 · 待同步 ' + outbox.length : '已连接 · rev ' + state.rev);
      renderAll();
    }).catch(function (e) {
      lastSyncError = e.message || String(e);
      offlineUntil = nowMs() + 15000;
      setConn(false, '离线 · 待同步 ' + outbox.length);
    });
  }

  function sync() {
    if (mode !== 'remote') { setConn(false, '本地模式'); return Promise.resolve(); }
    return flush().then(function () { return pull(); });
  }

  /* ---------- 渲染 ---------- */

  var KIND_CN = { fact: '事实', event: '事件', promise: '约定', clue: '线索', pref: '偏好', unresolved: '未解决', state: '状态' };

  function recordCard(r) {
    var live = r.status === 'active';
    var tag = KIND_CN[r.kind] || r.kind || '记忆';
    var h = '<div class="mem-card' + (live ? '' : ' dead') + '" onclick="memDetailOpen(\'' + esc(r.id) + '\')">';
    h += '<div class="mem-meta"><span class="mem-chip">' + esc(tag) + '</span>';
    if (r.topic) h += '<span class="mem-chip ghost">' + esc(r.topic) + '</span>';
    if (r.scope && r.scope !== 'main') h += '<span class="mem-chip ghost">' + esc(r.scope) + '</span>';
    h += '<span class="mem-day">' + esc(r.day || '') + '</span>';
    if (r.pending) h += '<span class="mem-chip warn">待同步</span>';
    h += '</div><div class="mem-text">' + esc(r.text) + '</div>';
    if (r.current_state) h += '<div class="mem-state">现状：' + esc(r.current_state) + '</div>';
    if (r.status === 'superseded') h += '<div class="mem-sub">已被新说法取代</div>';
    if (r.status === 'retracted') h += '<div class="mem-sub">已撤回' + (r.retractedReason ? ' · ' + esc(r.retractedReason) : '') + '</div>';
    if (live) {
      h += '<div class="mem-ops">' +
        '<button onclick="event.stopPropagation();memSupersede(\'' + esc(r.id) + '\')">变了</button>' +
        '<button onclick="event.stopPropagation();memCorrect(\'' + esc(r.id) + '\')">记错</button>' +
        '<button onclick="event.stopPropagation();memCleanupOne(\'' + esc(r.id) + '\')">删除</button>' +
        '</div>';
    }
    return h + '</div>';
  }

  function renderMemory() {
    var el = $('memList');
    if (!el) return;
    var list;
    if (searchHits) {
      list = searchHits.slice();
      el.innerHTML = '<div class="mem-sec">检索结果 · ' + list.length + ' 条</div>' +
        (list.length ? list.map(recordCard).join('') : '<div class="mem-empty">没有命中。</div>');
      return;
    }
    var live = state.records.filter(function (r) { return r.status === 'active'; });
    var dead = state.records.filter(function (r) {
      return r.status === 'superseded' || r.status === 'retracted' || r.status === 'closed';
    });
    live.sort(function (a, b) { return (b.ts || 0) - (a.ts || 0); });
    dead.sort(function (a, b) { return (b.ts || 0) - (a.ts || 0); });
    var h = '';
    if (outbox.length) h += '<div class="mem-sec">待同步 ' + outbox.length + ' 条</div>';
    h += '<div class="mem-sec">共享记忆 · ' + live.length + ' 条</div>';
    h += live.length ? live.slice(0, 100).map(recordCard).join('') : '<div class="mem-empty">还没有共享记忆。</div>';
    if (dead.length) h += '<div class="mem-sec">历史 · ' + dead.length + '</div>' + dead.slice(0, 30).map(recordCard).join('');
    el.innerHTML = h;
  }

  function renderUnresolved() {
    var el = $('memUnresolved');
    if (!el) return;
    var list = state.unresolved.slice().sort(function (a, b) { return (b.ts || 0) - (a.ts || 0); });
    el.innerHTML = list.length ? list.map(function (u) {
      var open = u.status !== 'closed';
      return '<div class="mem-card' + (open ? '' : ' dead') + '">' +
        '<div class="mem-meta"><span class="mem-chip">' + (open ? '未解决' : '已关闭') + '</span>' +
        '<span class="mem-day">' + esc(new Date(u.ts || nowMs()).toISOString().slice(0, 10)) + '</span></div>' +
        '<div class="mem-text">' + esc(u.summary) + '</div>' +
        (open ? '<div class="mem-ops"><button onclick="memResolve(\'' + esc(u.id) + '\',true)">解决了</button></div>' : '') +
        '</div>';
    }).join('') : '<div class="mem-empty">没有未解决的事。</div>';
  }

  function renderThreads() {
    var el = $('memThreads');
    if (!el) return;
    var list = state.threads.slice().sort(function (a, b) { return (b.ts || 0) - (a.ts || 0); });
    el.innerHTML = list.length ? list.map(function (t) {
      return '<div class="mem-card"><div class="mem-meta"><span class="mem-chip">会话</span>' +
        '<span class="mem-day">' + esc(t.day || '') + '</span></div>' +
        '<div class="mem-text">' + esc(t.summary || '（无摘要）') + '</div></div>';
    }).join('') : '<div class="mem-empty">还没有会话线索。</div>';
  }

  function renderPersona() {
    var el = $('memPersona');
    if (el && document.activeElement !== el) el.value = state.persona || '';
  }

  function renderAll() {
    renderMemory(); renderUnresolved(); renderThreads(); renderPersona();
    var n = state.records.filter(function (r) { return r.status === 'active'; }).length;
    if (booted) setConn(mode === 'remote', mode === 'remote'
      ? (outbox.length ? '已连接 · 待同步 ' + outbox.length : '已连接 · rev ' + state.rev)
      : '本地模式');
  }

  /* ---------- 对外操作 ---------- */

  function memAddOpen() { var s = $('memAddSheet'); if (s) s.classList.add('on'); }
  function memAddClose() { var s = $('memAddSheet'); if (s) s.classList.remove('on'); }
  function memAddSave() {
    var t = $('maText'), st = $('maState');
    var text = t ? String(t.value || '').trim() : '';
    if (!text) { if (window.toast) toast('先写点什么'); return; }
    enqueue('remember', { text: text, current_state: st ? String(st.value || '').trim() : '', kind: 'fact', topic: '' });
    if (t) t.value = '';
    if (st) st.value = '';
    memAddClose();
    if (window.toast) toast('已记进共享记忆');
  }

  function memSupersede(id) {
    var r = recById(id);
    if (!r) return;
    var text = window.prompt('新的说法是？（旧记录会保留变迁链）', r.text);
    if (!text || !String(text).trim()) return;
    var cur = window.prompt('现状补充（可留空）', r.current_state || '') || '';
    enqueue('supersede', { id: id, text: String(text).trim(), current_state: String(cur).trim() });
  }

  function memCorrect(id) {
    var reason = window.prompt('为什么记错了？（可不填）', '') || '';
    enqueue('correct', { id: id, reason: String(reason).trim() });
  }

  function memCleanupOne(id) {
    if (!window.confirm('彻底删掉这条？')) return;
    enqueue('forget', { id: id });
  }

  function memResolve(id, open) {
    enqueue('resolve', { id: id });
  }

  function memSearch() {
    var q = $('memQ');
    query = q ? String(q.value || '').trim() : '';
    /* 清空搜索框 = 回到全部记忆；空查询不要留在「0 条命中」的搜索结果页 */
    if (!query) { searchHits = null; renderMemory(); return; }
    if (mode === 'remote') {
      api('/memory/recall?q=' + encodeURIComponent(query) + '&k=20', null, 12000).then(function (res) {
        searchHits = res.items || [];
        renderMemory();
      }).catch(function () {
        searchHits = localSearch(query);
        renderMemory();
      });
    } else {
      searchHits = localSearch(query);
      renderMemory();
    }
  }

  function cnTerms(q) {
    q = String(q || '').toLowerCase();
    var out = (q.match(/[a-z0-9']+/g) || []);
    var cn = q.match(/[\u4e00-\u9fff]/g) || [];
    out = out.concat(cn);
    for (var i = 0; i < cn.length - 1; i++) out.push(cn[i] + cn[i + 1]);
    return out;
  }
  function localSearch(q) {
    var terms = cnTerms(q);
    if (!terms.length) return state.records.filter(function (r) { return r.status === 'active'; }).slice(0, 20);
    var scored = [];
    state.records.forEach(function (r) {
      if (r.status !== 'active') return;
      var t = (r.text + ' ' + (r.current_state || '')).toLowerCase();
      var s = 0;
      terms.forEach(function (x) { if (t.indexOf(x) >= 0) s += x.length > 1 ? 2 : 1; });
      if (s > 0) scored.push([s, r]);
    });
    scored.sort(function (a, b) { return b[0] - a[0]; });
    return scored.slice(0, 20).map(function (x) { return x[1]; });
  }

  function memSavePersona() {
    var el = $('memPersona');
    var p = el ? String(el.value || '') : '';
    state.persona = p;
    persist();
    if (mode === 'remote') {
      api('/persona', { persona: p }, 12000).then(function () {
        if (window.toast) toast('人格已保存到服务端');
      }).catch(function () {
        if (window.toast) toast('人格已保存在本机，联网后需再保存');
      });
    } else if (window.toast) toast('人格已保存在本机');
  }

  function memConnOpen() {
    var v = window.prompt('共享记忆服务地址（例如 http://192.168.1.20:8765）', base || 'http://127.0.0.1:8765');
    if (v === null) return;
    var nb = normalizeBase(v);
    if (!nb) return;
    base = nb;
    try { localStorage.setItem(BASE_KEY, base); } catch (e) {}
    var tok = window.prompt('访问 token（没设置就留空）', (function () {
      try { return localStorage.getItem('daddy.latent.token') || ''; } catch (e) { return ''; }
    })());
    if (tok !== null) {
      try { localStorage.setItem('daddy.latent.token', String(tok).trim()); } catch (e) {}
    }
    mode = 'remote';
    setConn(false, '连接中…');
    sync();
    setTimeout(function () {
      var t = $('memConnTxt');
      if (t && String(t.textContent || '').indexOf('连接中') >= 0) {
        setConn(false, '离线 · 待同步 ' + outbox.length);
      }
    }, 8000);
  }

  function memDetailOpen(id) {
    var r = recById(id);
    var s = $('memDetailSheet'), b = $('memDetailBody');
    if (!r || !s || !b) return;
    var rows = [
      ['类型', KIND_CN[r.kind] || r.kind || ''],
      ['主题', r.topic || ''],
      ['时间', r.day || ''],
      ['状态', r.status],
      ['原文', r.text],
      ['现状', r.current_state || ''],
      ['取代', r.supersedes || ''],
      ['被取代', r.supersededBy || ''],
      ['rev', String(r.rev == null ? '' : r.rev)]
    ];
    b.innerHTML = rows.filter(function (x) { return x[1]; }).map(function (x) {
      return '<div class="mem-kv"><span>' + esc(x[0]) + '</span><div>' + esc(x[1]) + '</div></div>';
    }).join('');
    s.classList.add('on');
  }
  function memDetailClose() { var s = $('memDetailSheet'); if (s) s.classList.remove('on'); }

  function memTab(t) {
    tab = t || 'memory';
    document.querySelectorAll('.mem-tab').forEach(function (b) {
      b.classList.toggle('on', b.getAttribute('data-tab') === tab);
    });
    ['memory', 'unresolved', 'threads', 'persona'].forEach(function (k) {
      var p = $('pane-' + k);
      if (p) p.hidden = k !== tab;
    });
    if (tab === 'memory') renderMemory();
    if (tab === 'unresolved') renderUnresolved();
    if (tab === 'threads') renderThreads();
    if (tab === 'persona') renderPersona();
  }

  function memLatentResolve(args) { return enqueue('resolve', args || {}); }
  function memLatentForget(args) { return enqueue('forget', args || {}); }
  function memLatentThreadClose(summary, scope) {
    return enqueue('thread_close', { summary: String(summary || ''), scope: normScope(scope || 'main') });
  }

  function memLatentRemember(text, kind, topic, currentState, scope) {
    text = String(text || '').trim();
    if (!text) return '';
    return enqueue('remember', {
      text: text, kind: kind || 'fact', topic: topic || '',
      current_state: currentState || '', scope: normScope(scope)
    });
  }
  function memLatentAppend(text, currentState) {
    return memLatentRemember(text, 'fact', '', currentState);
  }
  function memLatentConsolidate(scope, topic, text, currentState) {
    return enqueue('consolidate', {
      scope: normScope(scope), topic: String(topic || '').trim(),
      text: String(text || '').trim(), current_state: currentState || ''
    });
  }
  function memLatentSetState(scope, text) {
    return enqueue('state', { scope: normScope(scope), text: String(text || '') });
  }
  function memLatentWorkingState(scope) {
    var st = state.states[normScope(scope)];
    return st ? String(st.state_text || '') : '';
  }
  function memLatentStates() { return state.states; }
  function memLatentRecent(k, scope) {
    k = Math.max(1, Math.min(20, k || 8));
    var seen = {}, out = [];
    var useScope = scope == null ? null : normScope(scope);
    state.records.filter(function (r) { return r.status === 'active' && (!useScope || normScope(r.scope) === useScope); })
      .sort(function (a, b) { return (b.ts || 0) - (a.ts || 0); })
      .forEach(function (r) {
        var key = String(r.text || '') + '\u0001' + String(r.current_state || '');
        if (seen[key] || out.length >= k) return;
        seen[key] = 1;
        out.push('- (' + (r.day || '') + ') ' + r.text + (r.current_state ? '（现状：' + r.current_state + '）' : ''));
      });
    return out;
  }
  function memLatentState() { return state; }
  function memLatentPending() { return outbox.length; }

  function memLatentRecall(q, k, scope) {
    q = String(q || '').trim();
    // null = 这次没有可用的服务端结果（没配置 / 离线 / 空查询），调用方应回退本机缓存；
    // [] = 服务端确实查过但没有命中，调用方应把空结果当作答案。
    if (!q) return Promise.resolve(null);
    if (mode === 'remote') {
      if (nowMs() < offlineUntil) return Promise.reject(new Error('offline'));
      var u = '/memory/recall?q=' + encodeURIComponent(q) + '&k=' + (k || 6);
      if (scope != null) u += '&scope=' + encodeURIComponent(normScope(scope));
      return api(u, null, 3000).then(function (res) {
        var items = res.items || [];
        return items.map(function (r) {
          return '- (' + (r.day || '') + ') ' + r.text + (r.current_state ? '（现状：' + r.current_state + '）' : '');
        });
      });
    }
    return Promise.resolve(null);
  }

  function memLatentBoot() {
    base = '';
    try { base = localStorage.getItem(BASE_KEY) || ''; } catch (e) {}
    if (!base) {
      /* 页面本身由 latent-server 提供时，同源就是记忆服务；
         这样从局域网里另一台设备打开 http://192.168.x.x:8765 也不会指向它自己的 127.0.0.1。 */
      try {
        if (/^https?:$/.test(location.protocol)) base = location.origin;
      } catch (e) {}
    }
    if (!base) base = 'http://127.0.0.1:8765';
    base = normalizeBase(base);
    openDB().then(function () {
      return Promise.all([dbGet('state'), obAll()]);
    }).then(function (arr) {
      var cached = arr[0], ops = arr[1] || [];
      if (cached && cached.records) {
        state = {
          records: cached.records || [], unresolved: cached.unresolved || [],
          threads: cached.threads || [], states: cached.states || {},
          persona: cached.persona || '',
          rev: cached.rev || 0
        };
      }
      try {
        var rv = parseInt(localStorage.getItem(LAST_REV_KEY) || '0', 10);
        if (rv > state.rev) state.rev = rv;
      } catch (e) {}
      outbox = ops.filter(function (o) { return o && o.op_id; }).sort(function (a, b) { return (a.created || 0) - (b.created || 0); });
      outbox.forEach(function (o) { if (o.type) localApply(o); });
      booted = true;
      mode = 'remote';
      renderAll();
      setConn(false, '连接中…');
      sync();
      /* 看门狗：连接流程若被 IndexedDB/网络卡住，也要给出可读状态，不能永远停在“连接中” */
      setTimeout(function () {
        var t = $('memConnTxt');
        if (t && String(t.textContent || '').indexOf('连接中') >= 0) {
          setConn(false, '离线 · 待同步 ' + outbox.length);
        }
      }, 8000);
      setInterval(function () { if (document.visibilityState !== 'hidden') sync(); }, 25000);
      document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'visible') sync();
      });
    }).catch(function (e) {
      booted = true;
      setConn(false, '本地模式');
      renderAll();
      lastSyncError = e.message || String(e);
    });
  }

  window.memLatentBoot = memLatentBoot;
  window.memLatentState = memLatentState;
  window.memLatentRecent = memLatentRecent;
  window.memLatentAppend = memLatentAppend;
  window.memLatentRemember = memLatentRemember;
  window.memLatentConsolidate = memLatentConsolidate;
  window.memLatentSetState = memLatentSetState;
  window.memLatentWorkingState = memLatentWorkingState;
  window.memLatentStates = memLatentStates;
  window.memLatentResolve = memLatentResolve;
  window.memLatentForget = memLatentForget;
  window.memLatentThreadClose = memLatentThreadClose;
  window.memLatentRecall = memLatentRecall;
  window.memLatentPending = memLatentPending;
  window.memLatentSync = sync;
  window.memTab = memTab;
  window.memAddOpen = memAddOpen;
  window.memAddClose = memAddClose;
  window.memAddSave = memAddSave;
  window.memSupersede = memSupersede;
  window.memCorrect = memCorrect;
  window.memCleanupOne = memCleanupOne;
  window.memSearch = memSearch;
  window.memResolve = memResolve;
  window.memSavePersona = memSavePersona;
  window.memConnOpen = memConnOpen;
  window.memDetailOpen = memDetailOpen;
  window.memDetailClose = memDetailClose;
})();
