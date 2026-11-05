/* mem-latent.js · Latent 风格共享记忆（本地 IndexedDB + 可选 HTTP 服务） */
(function () {
  'use strict';

  var DB_NAME = 'caeruleus-latent.v1';
  var BASE_KEY = 'daddy.latent.base';
  var state = { records: [], unresolved: [], threads: [], persona: '' };
  var mode = 'local';           // local | remote
  var base = '';
  var db = null;
  var tab = 'memory';
  var query = '';
  var searchHits = null;        // null = 正常列表

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }
  function toast(msg) { if (window.toast) window.toast(msg); }

  /* ---------- IndexedDB ---------- */
  function openDB() {
    return new Promise(function (res, rej) {
      if (db) return res(db);
      if (typeof indexedDB === 'undefined') return rej(new Error('no indexedDB'));
      var req = indexedDB.open(DB_NAME, 1);
      req.onupgradeneeded = function () {
        var d = req.result;
        if (!d.objectStoreNames.contains('kv')) d.createObjectStore('kv', { keyPath: 'k' });
      };
      req.onsuccess = function () { db = req.result; res(db); };
      req.onerror = function () { rej(req.error); };
    });
  }
  function dbGet(k) {
    return openDB().then(function (d) {
      return new Promise(function (res) {
        var t = d.transaction('kv', 'readonly').objectStore('kv').get(k);
        t.onsuccess = function () { res(t.result ? t.result.v : null); };
        t.onerror = function () { res(null); };
      });
    }).catch(function () { return null; });
  }
  function dbSet(k, v) {
    return openDB().then(function (d) {
      return new Promise(function (res) {
        var t = d.transaction('kv', 'readwrite').objectStore('kv').put({ k: k, v: v });
        t.onsuccess = function () { res(true); };
        t.onerror = function () { res(false); };
      });
    }).catch(function () { return false; });
  }

  function persist() { return dbSet('state', state); }

  /* ---------- 远程 ---------- */
  function api(path, body, timeout) {
    var url = base.replace(/\/+$/, '') + path;
    var opt = { method: body ? 'POST' : 'GET', headers: {} };
    if (body) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    if (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) {
      opt.signal = AbortSignal.timeout(timeout || 8000);
    }
    return fetch(url, opt).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }

  function setConn() {
    var dot = $('memConnDot'), txt = $('memConnTxt');
    if (!dot || !txt) return;
    if (mode === 'remote') {
      dot.className = 'dot live';
      txt.textContent = '已连接 · ' + base.replace(/^https?:\/\//, '');
    } else {
      dot.className = 'dot';
      txt.textContent = base ? '未连上 · ' + base.replace(/^https?:\/\//, '') + '（用本地）' : '本地模式';
    }
  }

  function pullRemote() {
    if (!base) return Promise.resolve(false);
    return api('/state', null, 5000).then(function (s) {
      if (s && s.records) {
        var remote = s.records || [];
        var ids = {};
        remote.forEach(function (r) { ids[r.id] = 1; });
        var localOnly = state.records.filter(function (r) {
          return !ids[r.id] && (r.status || 'active') === 'active';
        });
        state = {
          records: remote.concat(localOnly),
          unresolved: s.unresolved || [], threads: s.threads || [], persona: s.persona || ''
        };
        mode = 'remote';
        return persist().then(function () {
          if (!localOnly.length) return true;
          var chain = Promise.resolve();
          localOnly.forEach(function (r) {
            chain = chain.then(function () {
              return api('/append', {
                text: r.text, current_state: r.current_state || '',
                indexEvidence: r.evidence || []
              }, 6000).then(function (res) {
                if (res && res.record) {
                  var i = state.records.findIndex(function (x) { return x.id === r.id; });
                  if (i >= 0) state.records[i] = res.record;
                  else state.records.push(res.record);
                }
              }).catch(function () {});
            });
          });
          return chain.then(function () { return persist(); }).then(function () { return true; });
        });
      }
      return false;
    }).catch(function () { mode = 'local'; return false; });
  }

  /* ---------- 渲染 ---------- */
  function statusOf(r) { return r.status || 'active'; }
  function statusText(r) {
    var s = statusOf(r);
    if (s === 'superseded') return '已取代';
    if (s === 'retracted') return '已撤回';
    return '现行';
  }
  function dayText(ts) {
    var d = new Date(ts || Date.now());
    return (d.getMonth() + 1) + ' 月 ' + d.getDate() + ' 日';
  }

  function recordCard(r, opts) {
    opts = opts || {};
    var s = statusOf(r);
    var chain = '';
    if (r.supersedes) chain += '<span class="mem-chain">← 取代了旧记录</span>';
    if (r.supersededBy) chain += '<span class="mem-chain">→ 已由新记录取代</span>';
    return '<div class="mem-card' + (s === 'active' ? '' : ' dim') + '" data-id="' + esc(r.id) + '">'
      + '<div class="mem-card-top"><span class="mem-day">' + dayText(r.ts) + '</span>'
      + '<span class="mem-st ' + s + '">' + statusText(r) + '</span>'
      + (opts.score ? '<span class="mem-score">' + Number(opts.score).toFixed(1) + '</span>' : '')
      + '</div>'
      + '<div class="mem-text">' + esc(r.text) + '</div>'
      + (r.current_state ? '<div class="mem-cur">现在：' + esc(r.current_state) + '</div>' : '')
      + (chain ? '<div class="mem-meta">' + chain + '</div>' : '')
      + '<div class="mem-acts">'
      + (s === 'active'
        ? '<button onclick="memDetailOpen(\'' + esc(r.id) + '\')">详情</button>'
          + '<button onclick="memSupersede(\'' + esc(r.id) + '\')">变了</button>'
          + '<button onclick="memCorrect(\'' + esc(r.id) + '\')">记错</button>'
        : '<button onclick="memDetailOpen(\'' + esc(r.id) + '\')">详情</button>'
          + (s === 'retracted' ? '<button onclick="memCleanupOne(\'' + esc(r.id) + '\')">删除</button>' : ''))
      + '</div></div>';
  }

  function renderMemory() {
    var el = $('memList');
    if (!el) return;
    if (searchHits) {
      if (!searchHits.length) {
        el.innerHTML = '<div class="mem-empty"><b>没有找到。</b><span>换个说法，或补一个名字、地点。</span></div>';
        return;
      }
      el.innerHTML = searchHits.map(function (r) { return recordCard(r, { score: r.score }); }).join('');
      return;
    }
    var live = state.records.filter(function (r) { return statusOf(r) === 'active'; });
    var old = state.records.filter(function (r) { return statusOf(r) !== 'active'; });
    var html = '';
    if (!live.length && !old.length) {
      el.innerHTML = '<div class="mem-empty"><b>还没有记下什么。</b><span>聊到值得留下的事，点上面的「记一件事」。</span></div>';
      return;
    }
    if (live.length) html += '<div class="mem-sec">现行 · ' + live.length + '</div>' + live.slice().sort(function (a, b) { return (b.ts || 0) - (a.ts || 0); }).map(function (r) { return recordCard(r); }).join('');
    if (old.length) html += '<div class="mem-sec">历史 · ' + old.length + '</div>' + old.slice().sort(function (a, b) { return (b.ts || 0) - (a.ts || 0); }).map(function (r) { return recordCard(r); }).join('');
    el.innerHTML = html;
  }

  function renderUnresolved() {
    var el = $('memUnresolved');
    if (!el) return;
    var items = state.unresolved || [];
    if (!items.length) {
      el.innerHTML = '<div class="mem-empty"><b>没有未解决的事。</b><span>答应过、还没做完的，会留在这里。</span></div>';
      return;
    }
    el.innerHTML = '<div class="mem-sec">仍没结束 · ' + items.filter(function (u) { return u.status !== 'closed'; }).length + '</div>'
      + items.map(function (u) {
        var closed = u.status === 'closed';
        return '<div class="mem-card' + (closed ? ' dim' : '') + '">'
          + '<div class="mem-text">' + esc(u.summary) + '</div>'
          + '<div class="mem-meta"><span>' + (closed ? '已结束' : '进行中') + '</span></div>'
          + '<div class="mem-acts"><button onclick="memResolve(\'' + esc(u.id) + '\',' + (!closed) + ')">' + (closed ? '重新打开' : '标记结束') + '</button></div>'
          + '</div>';
      }).join('');
  }

  function renderThreads() {
    var el = $('memThreads');
    if (!el) return;
    var items = (state.threads || []).slice().reverse();
    if (!items.length) {
      el.innerHTML = '<div class="mem-empty"><b>还没有会话线索。</b><span>每次关掉窗口，可以在这里留下一句「上次聊到哪」。</span></div>';
      return;
    }
    el.innerHTML = items.map(function (t) {
      return '<div class="mem-card"><div class="mem-card-top"><span class="mem-day">' + dayText(t.ts) + '</span></div>'
        + '<div class="mem-text">' + esc(t.summary) + '</div></div>';
    }).join('');
  }

  function renderPersona() {
    var el = $('memPersona');
    if (el && el.value !== (state.persona || '')) el.value = state.persona || '';
  }

  function renderAll() {
    renderMemory(); renderUnresolved(); renderThreads(); renderPersona(); setConn();
  }

  /* ---------- 标签切换 ---------- */
  function memTab(name) {
    tab = name;
    var panes = { memory: 'pane-memory', unresolved: 'pane-unresolved', threads: 'pane-threads', persona: 'pane-persona' };
    Object.keys(panes).forEach(function (k) {
      var p = $(panes[k]); if (p) p.hidden = (k !== name);
    });
    var tabs = document.querySelectorAll('.mem-tab');
    for (var i = 0; i < tabs.length; i++) tabs[i].classList.toggle('on', tabs[i].getAttribute('data-tab') === name);
  }

  /* ---------- 操作 ---------- */
  function memAddOpen() { var m = $('memAddMask'), s = $('memAddSheet'); if (!m || !s) return; m.classList.add('on'); s.classList.add('on'); setTimeout(function () { var t = $('maText'); if (t) t.focus(); }, 120); }
  function memAddClose() { var m = $('memAddMask'), s = $('memAddSheet'); if (m) m.classList.remove('on'); if (s) s.classList.remove('on'); }

  function newLocalRecord(payload) {
    return {
      id: Math.random().toString(16).slice(2, 18), ts: Date.now(), day: new Date().toISOString().slice(0, 10),
      text: payload.text, current_state: payload.current_state || '', evidence: payload.indexEvidence || [],
      status: 'active', supersedes: '', supersededBy: '', weight: 1, hits: 0, retractedReason: ''
    };
  }
  function pushRecord(payload) {
    if (mode === 'remote') {
      return api('/append', payload, 10000).then(function (r) {
        if (!r.ok) throw new Error(r.error || '写入失败');
        return r;
      }).catch(function () {
        mode = 'local'; setConn();
        var rec = newLocalRecord(payload);
        state.records.push(rec);
        return persist().then(function () { toast('连不上，先存本机'); return { ok: true, record: rec, local: true }; });
      });
    }
    var rec = newLocalRecord(payload);
    state.records.push(rec);
    return persist().then(function () { return { ok: true, record: rec }; });
  }

  function memAddSave() {
    var t = $('maText'), c = $('maState');
    var text = t ? t.value.trim() : '';
    if (!text) { toast('先写点什么'); return; }
    var payload = { text: text, current_state: c ? c.value.trim() : '' };
    pushRecord(payload).then(function (r) {
      var rec = r.record;
      if (mode === 'remote') {
        var i = state.records.findIndex(function (x) { return x.id === rec.id; });
        if (i < 0) state.records.push(rec); else state.records[i] = rec;
      }
      if (t) t.value = ''; if (c) c.value = '';
      memAddClose();
      renderMemory();
      toast(mode === 'remote' ? '已写入 · 其他客户端可见' : '已记下 · 仅这台设备');
    }).catch(function (e) { toast('没写进去：' + e.message); });
  }

  function memSupersede(id) {
    var r = state.records.filter(function (x) { return x.id === id; })[0];
    if (!r) return;
    var text = prompt('这件事现在变成了什么？', '');
    if (text == null) return;
    text = text.trim();
    if (!text) return;
    var cur = prompt('现在的状态（可空）：', '') || '';
    if (mode === 'remote') {
      api('/supersede', { supersedes: id, text: text, current_state: cur }, 10000).then(function (r2) {
        if (!r2.ok) throw new Error(r2.error || '写入失败');
        r.status = 'superseded'; r.supersededBy = r2.record.id;
        state.records.push(r2.record);
        return persist();
      }).then(function () { renderMemory(); toast('已记下变化'); })
        .catch(function (e) { toast('失败：' + e.message); });
    } else {
      r.status = 'superseded';
      var nid = Math.random().toString(16).slice(2, 18);
      r.supersededBy = nid;
      state.records.push({ id: nid, ts: Date.now(), day: new Date().toISOString().slice(0, 10), text: text, current_state: cur, evidence: [], status: 'active', supersedes: id, supersededBy: '', weight: 1, hits: 0, retractedReason: '' });
      persist(); renderMemory(); toast('已记下变化');
    }
  }

  function memCorrect(id) {
    var r = state.records.filter(function (x) { return x.id === id; })[0];
    if (!r) return;
    var why = prompt('哪里记错了？', '');
    if (why == null) return;
    if (mode === 'remote') {
      api('/correct', { recordId: id, reason: why }, 10000).then(function (r2) {
        if (!r2.ok) throw new Error(r2.error || '失败');
        r.status = 'retracted'; r.retractedReason = why;
        return persist();
      }).then(function () { renderMemory(); toast('已撤回'); })
        .catch(function (e) { toast('失败：' + e.message); });
    } else {
      r.status = 'retracted'; r.retractedReason = why;
      persist(); renderMemory(); toast('已撤回');
    }
  }

  function memCleanupOne(id) {
    if (!confirm('彻底删除这条记录？')) return;
    if (mode === 'remote') {
      api('/cleanup', { recordIds: [id] }, 8000).then(function () {
        state.records = state.records.filter(function (x) { return x.id !== id; });
        return persist();
      }).then(renderMemory).catch(function (e) { toast('失败：' + e.message); });
    } else {
      state.records = state.records.filter(function (x) { return x.id !== id; });
      persist(); renderMemory();
    }
  }

  function memSearch() {
    var q = $('memQ');
    query = q ? q.value.trim() : '';
    if (!query) { searchHits = null; renderMemory(); return; }
    if (mode === 'remote') {
      api('/search', { query: query, topN: 20 }, 8000).then(function (r) {
        searchHits = r.items || []; renderMemory();
      }).catch(function () { localSearch(); });
    } else localSearch();
  }
  function cnTerms(s) {
    s = String(s || '').toLowerCase();
    var out = {}, i;
    (s.match(/[a-z0-9']+/g) || []).forEach(function (w) { out[w] = 1; });
    (s.match(/[\u4e00-\u9fa5]+/g) || []).forEach(function (seg) {
      for (i = 0; i < seg.length; i++) {
        out[seg.charAt(i)] = 1;
        if (i < seg.length - 1) out[seg.substr(i, 2)] = 1;
      }
    });
    return Object.keys(out);
  }
  function scoreRecord(r, terms) {
    var s = ((r.text || '') + ' ' + (r.current_state || '')).toLowerCase();
    var sc = 0;
    for (var i = 0; i < terms.length; i++) {
      if (s.indexOf(terms[i]) >= 0) sc += terms[i].length >= 2 ? 2 : 1;
    }
    return sc;
  }
  function localSearch() {
    var terms = cnTerms(query);
    var scored = state.records.filter(function (r) { return statusOf(r) === 'active'; }).map(function (r) {
      return { r: r, sc: scoreRecord(r, terms) };
    }).filter(function (x) { return x.sc > 0; });
    scored.sort(function (a, b) { return b.sc - a.sc || (b.r.ts || 0) - (a.r.ts || 0); });
    searchHits = scored.slice(0, 50).map(function (x) { return x.r; });
    renderMemory();
  }

  function memResolve(id, open) {
    var item = (state.unresolved || []).filter(function (u) { return u.id === id; })[0];
    if (!item) return;
    item.status = open ? 'closed' : 'open';
    if (mode === 'remote') {
      api('/unresolved', { ops: [{ action: open ? 'close' : 'open', id: id, summary: item.summary }] }, 8000)
        .then(function () { return persist(); }).then(renderUnresolved)
        .catch(function (e) { toast('失败：' + e.message); });
    } else { persist(); renderUnresolved(); }
  }

  function memSavePersona() {
    var el = $('memPersona'); if (!el) return;
    state.persona = el.value;
    if (mode === 'remote') {
      fetch(base.replace(/\/+$/, '') + '/persona', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ persona: state.persona }) })
        .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return persist(); })
        .then(function () { toast('人格已保存'); })
        .catch(function (e) { toast('失败：' + e.message); });
    } else { persist(); toast('人格已保存在这台设备'); }
  }

  function memConnOpen() {
    var v = prompt('记忆服务地址（留空 = 只用本地）', base || 'http://127.0.0.1:8765');
    if (v == null) return;
    v = v.trim();
    base = v;
    try { localStorage.setItem(BASE_KEY, v); } catch (e) {}
    if (!v) { mode = 'local'; setConn(); toast('已切回本地模式'); return; }
    pullRemote().then(function (ok) {
      if (ok) { renderAll(); toast('已连上 · 其他客户端看到的是同一份'); }
      else { setConn(); toast('连不上，先用本地'); }
    });
  }

  /* ---------- 详情 ---------- */
  function memDetailOpen(id) {
    var r = state.records.filter(function (x) { return x.id === id; })[0];
    if (!r) return;
    var el = $('memDetailBody');
    if (!el) return;
    var chainHtml = '';
    if (r.supersedes) {
      var prev = state.records.filter(function (x) { return x.id === r.supersedes; })[0];
      if (prev) chainHtml += '<div class="mem-chain-row">此前：' + esc(prev.text) + '</div>';
    }
    if (r.supersededBy) {
      var next = state.records.filter(function (x) { return x.id === r.supersededBy; })[0];
      if (next) chainHtml += '<div class="mem-chain-row">后来：' + esc(next.text) + '</div>';
    }
    el.innerHTML = '<div class="mem-detail-day">' + dayText(r.ts) + ' · ' + statusText(r) + '</div>'
      + '<div class="mem-detail-text">' + esc(r.text) + '</div>'
      + (r.current_state ? '<div class="mem-detail-cur">现在：' + esc(r.current_state) + '</div>' : '')
      + (r.retractedReason ? '<div class="mem-detail-cur">撤回原因：' + esc(r.retractedReason) + '</div>' : '')
      + (chainHtml ? '<div class="mem-detail-chain">' + chainHtml + '</div>' : '')
      + '<div class="mem-detail-acts">'
      + '<button onclick="memDetailClose()">收起来</button>'
      + (statusOf(r) === 'active' ? '<button onclick="memDetailClose();memSupersede(\'' + r.id + '\')">变了</button>' : '')
      + (statusOf(r) === 'active' ? '<button onclick="memDetailClose();memCorrect(\'' + r.id + '\')">记错</button>' : '')
      + '</div>';
    $('memDetailMask').classList.add('on');
    $('memDetailSheet').classList.add('on');
  }
  function memDetailClose() {
    var m = $('memDetailMask'), s = $('memDetailSheet');
    if (m) m.classList.remove('on'); if (s) s.classList.remove('on');
  }

  /* ---------- 启动 ---------- */
  function memLatentBoot() {
    try { base = localStorage.getItem(BASE_KEY) || 'http://127.0.0.1:8765'; } catch (e) { base = 'http://127.0.0.1:8765'; }
    return dbGet('state').then(function (saved) {
      if (saved && saved.records) state = saved;
      renderAll();
      if (!base) return false;
      return pullRemote().then(function (ok) { if (ok) renderAll(); return ok; });
    }).catch(function () { renderAll(); });
  }

  window.memLatentBoot = memLatentBoot;
  window.memLatentState = function () { return state; };
  window.memLatentRecall = function (q, k) {
    var terms = cnTerms(q);
    if (!terms.length) return '';
    var scored = state.records.filter(function (r) { return statusOf(r) === 'active'; }).map(function (r) {
      return { r: r, sc: scoreRecord(r, terms) };
    }).filter(function (x) { return x.sc > 0; });
    scored.sort(function (a, b) { return b.sc - a.sc || (b.r.ts || 0) - (a.r.ts || 0); });
    return scored.slice(0, k || 4).map(function (x) {
      var r = x.r;
      return '- (' + dayText(r.ts) + ') ' + r.text + (r.current_state ? '（现在：' + r.current_state + '）' : '');
    }).join('\n');
  };
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
