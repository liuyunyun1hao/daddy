/* ============================================================
   Caeruleus · 本地向量记忆（mem-local.js）
   数据与向量只存在这台设备的 IndexedDB；
   Embedding 接口只把文字变成向量，不保存任何内容。
   没配置接口时，自动退回原有的 BM25 关键词检索。
   ============================================================ */
(function () {
  'use strict';

  var DB_NAME = 'caeruleus-mem.v1';
  var DB_VER = 1;
  var dbPromise = null;
  var VEC_CACHE = new Map();
  var QUEUE = [];
  var BUSY = false;
  var TRIED = {};
  var saveTimer = 0;
  var SAVED = {};
  var BOOTED = false;

  /* ---------- IndexedDB ---------- */
  function openDB() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise(function (res) {
      if (!window.indexedDB) return res(null);
      var req = indexedDB.open(DB_NAME, DB_VER);
      req.onupgradeneeded = function () {
        var db = req.result;
        if (!db.objectStoreNames.contains('items')) db.createObjectStore('items', { keyPath: 'id' });
        if (!db.objectStoreNames.contains('meta')) db.createObjectStore('meta', { keyPath: 'k' });
      };
      req.onsuccess = function () { res(req.result); };
      req.onerror = function () { res(null); };
      req.onblocked = function () { res(null); };
    });
    return dbPromise;
  }

  function dbAll() {
    return openDB().then(function (db) {
      if (!db) return [];
      return new Promise(function (res) {
        var t = db.transaction('items', 'readonly');
        var r = t.objectStore('items').getAll();
        r.onsuccess = function () { res(r.result || []); };
        r.onerror = function () { res([]); };
      });
    });
  }

  function dbPutAll(items) {
    return openDB().then(function (db) {
      if (!db) return;
      return new Promise(function (res) {
        var t = db.transaction('items', 'readwrite');
        var s = t.objectStore('items');
        try { s.clear(); } catch (e) {}
        (items || []).forEach(function (x) { try { s.put(x); } catch (e) {} });
        t.oncomplete = function () { res(); };
        t.onerror = function () { res(); };
        t.onabort = function () { res(); };
      });
    });
  }

  function dbPutOne(it) {
    return openDB().then(function (db) {
      if (!db) return;
      return new Promise(function (res) {
        var t = db.transaction('items', 'readwrite');
        try { t.objectStore('items').put(it); } catch (e) {}
        t.oncomplete = function () { res(); };
        t.onerror = function () { res(); };
      });
    });
  }

  /* ---------- 向量接口 ---------- */
  function cfgObj() {
    try { if (typeof cfg !== 'undefined' && cfg) return cfg; } catch (e) {}
    return window.cfg || {};
  }

  function embReady() {
    var c = cfgObj();
    if (!c.embOn || !c.embUrl || !c.embModel) return false;
    if (!c.embKey && !/^https?:\/\/(127\.0\.0\.1|localhost)/i.test(c.embUrl)) return false;
    return true;
  }

  function embURL() {
    var u = String(cfgObj().embUrl || '').trim().replace(/\/+$/, '');
    if (!u) return '';
    if (/\/embeddings$/i.test(u)) return u;
    if (/\/v\d+(?:beta)?$/i.test(u)) return u + '/embeddings';
    return u + '/v1/embeddings';
  }

  function normalize(v) {
    var s = 0, i;
    for (i = 0; i < v.length; i++) s += v[i] * v[i];
    s = Math.sqrt(s) || 1;
    for (i = 0; i < v.length; i++) v[i] /= s;
    return v;
  }

  function dot(a, b) {
    var s = 0, n = Math.min(a.length, b.length);
    for (var i = 0; i < n; i++) s += a[i] * b[i];
    return s;
  }

  function withTimeout(p, ms) {
    return new Promise(function (res, rej) {
      var to = setTimeout(function () { rej(new Error('timeout')); }, ms);
      p.then(function (x) { clearTimeout(to); res(x); }, function (e) { clearTimeout(to); rej(e); });
    });
  }

  function embedText(text, isQuery) {
    var c = cfgObj();
    var t = String(text || '').trim();
    if (!t || !embReady()) return Promise.resolve(null);
    t = t.slice(0, 1800);
    var key = (isQuery ? 'q:' : 'd:') + t;
    if (VEC_CACHE.has(key)) return Promise.resolve(VEC_CACHE.get(key));

    var body = { model: c.embModel, input: t };
    if (c.embDims) body.dimensions = Number(c.embDims);
    var headers = { 'Content-Type': 'application/json' };
    if (c.embKey) headers['Authorization'] = 'Bearer ' + c.embKey;

    return fetch(embURL(), { method: 'POST', headers: headers, body: JSON.stringify(body) })
      .then(function (r) {
        if (!r.ok) {
          return r.text().then(function (x) {
            throw new Error('HTTP ' + r.status + (x ? ' · ' + String(x).slice(0, 90) : ''));
          });
        }
        return r.json();
      })
      .then(function (j) {
        var d = j && j.data && j.data[0];
        var arr = d && (d.embedding || d.vector);
        if (!arr || !arr.length) throw new Error('接口没有返回向量');
        var v = normalize(new Float32Array(arr));
        VEC_CACHE.set(key, v);
        if (VEC_CACHE.size > 80) VEC_CACHE.delete(VEC_CACHE.keys().next().value);
        return v;
      });
  }

  /* ---------- 后台向量化队列 ---------- */
  function queue(id) {
    if (!embReady() || TRIED[id]) return;
    if (QUEUE.indexOf(id) < 0) QUEUE.push(id);
    pump();
  }

  function pump() {
    if (BUSY || !embReady() || !QUEUE.length) return;
    BUSY = true;
    var id = QUEUE.shift();
    var it = (window.brainLoad().items || []).filter(function (x) { return x.id === id; })[0];
    if (!it || it.vec) {
      BUSY = false;
      setTimeout(pump, 60);
      return;
    }
    embedText(it.text, false).then(function (v) {
      if (!v) return;
      it.vec = v;
      it.vecModel = cfgObj().embModel;
      it.vecAt = Date.now();
      return dbPutOne(it);
    }).catch(function () {
      TRIED[id] = 1;
    }).then(function () {
      BUSY = false;
      paintVecState();
      setTimeout(pump, 120);
    });
  }

  /* ---------- 与页面主脚本的衔接 ---------- */
  /* brainSave() 里调用：把最新条目落进 IndexedDB（含向量），
     localStorage 那份由主脚本剥离向量后保存。 */
  function memOnSave() {
    if (!window.brainLoad) return;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function () {
      var items = (window.brainLoad().items || []);
      openDB().then(function (db) {
        if (!db) return;
        var t = db.transaction('items', 'readwrite');
        var st = t.objectStore('items');
        var keep = {}, changed = [];
        items.forEach(function (x) {
          keep[x.id] = 1;
          var old = SAVED[x.id];
          if (!old || old.ts !== x.ts || old.vecAt !== x.vecAt || old.text !== x.text) {
            try { st.put(x); } catch (e) {}
            changed.push(x);
          }
        });
        var keys = st.getAllKeys();
        keys.onsuccess = function () {
          if (!BOOTED) return;
          (keys.result || []).forEach(function (k) { if (!keep[k]) { try { st.delete(k); } catch (e) {} } });
        };
        t.oncomplete = function () {
          changed.forEach(function (x) { SAVED[x.id] = { ts: x.ts, vecAt: x.vecAt, text: x.text }; });
          paintVecState();
        };
        t.onerror = function () {};
      }).catch(function () {});
      if (embReady()) items.forEach(function (x) { if (!x.vec) queue(x.id); });
      paintVecState();
    }, 800);
  }

  /* 启动：把 IndexedDB 合并回内存；DB 空则迁移 localStorage。 */
  function memBoot() {
    if (!window.indexedDB) { BOOTED = true; return; }
    openDB().then(function (db) {
      if (!db) return;
      return dbAll().then(function (rows) {
        var b = window.brainLoad();
        if (!rows.length) {
          if (b.items && b.items.length) return dbPutAll(b.items);
          return;
        }
        var byId = {}, order = [];
        rows.forEach(function (x) { byId[x.id] = x; SAVED[x.id] = { ts: x.ts, vecAt: x.vecAt, text: x.text }; });
        (b.items || []).forEach(function (x) {
          if (!byId[x.id]) { byId[x.id] = x; order.push(x.id); }
          else if (byId[x.id].vec && !x.vec) x.vec = byId[x.id].vec;
        });
        var merged = Object.keys(byId).map(function (k) { return byId[k]; })
          .sort(function (a, c) { return (a.ts || 0) - (c.ts || 0); });
        b.items = merged;
        b.seq = Math.max(b.seq || 0, merged.reduce(function (m, x) {
          var n = parseInt(String(x.id || '').slice(1), 10) || 0;
          return n > m ? n : m;
        }, 0));
        merged.forEach(function (x) { if (!x.vec) queue(x.id); });
        if (typeof window.renderBrainList === 'function' && document.getElementById('tl')) {
          try { window.renderBrainList(); } catch (e) {}
        }
        paintVecState();
      });
    }).then(function () { BOOTED = true; }).catch(function (e) { BOOTED = true; console.warn('[mem] boot', e); });
  }

  /* 混合召回：最近 / BM25 / 向量，合并去重。 */
  function memRecallBlock(q, k, ms) {
    var items = (window.brainLoad().items || []).filter(function (x) {
      return !x.revoked && !x.supersededBy;
    });
    if (!items.length) return Promise.resolve('');
    var out = [], seen = {};
    function push(x, s) {
      if (!x || seen[x.id]) return;
      seen[x.id] = 1;
      out.push({ x: x, s: s });
    }
    items.slice().sort(function (a, b) { return (b.ts || 0) - (a.ts || 0); })
      .slice(0, 2).forEach(function (x) { push(x, 0.2); });
    if (window.brainSearch) {
      window.brainSearch(q, k || 6).forEach(function (x) { push(x, 1); });
    }

    var p = Promise.resolve();
    if (embReady()) {
      p = withTimeout(embedText(q, true), ms || 1600).then(function (qv) {
        if (!qv) return;
        items.forEach(function (x) {
          if (!x.vec || !x.vec.length || x.vec.length !== qv.length) return;
          var c = dot(qv, x.vec);
          if (c > 0.28) push(x, 1.5 + c);
        });
      }).catch(function () {});
    }

    return p.then(function () {
      out.sort(function (a, b) { return b.s - a.s; });
      var top = out.slice(0, k || 6);
      if (!top.length) return '';
      return '【你记得的事 · 存在这台设备上】\n' + top.map(function (o) {
        var d = new Date(o.x.ts);
        return '- (' + (d.getMonth() + 1) + '.' + d.getDate() + ') ' + o.x.text;
      }).join('\n');
    });
  }

  /* ---------- 界面 ---------- */
  function paintVecState() {
    if (!window.brainLoad || typeof document === 'undefined') return;
    var items = window.brainLoad().items || [];
    var done = 0;
    items.forEach(function (x) { if (x.vec) done++; });
    var c = document.getElementById('embCount');
    if (c) c.textContent = done + ' / ' + items.length;
    var f = document.getElementById('foldVecV');
    if (f) f.textContent = cfgObj().embOn ? (done + '/' + items.length + ' 已向量化') : '未启用';
    var st = document.getElementById('embState');
    if (st && !cfgObj().embOn) st.textContent = '未启用 ›';
  }

  function toggleEmb(el) {
    var c = cfgObj();
    c.embOn = !c.embOn;
    if (el) el.classList.toggle('on', !!c.embOn);
    if (window.saveCfg) window.saveCfg();
    if (window.toast) window.toast(c.embOn ? '已开启向量记忆' : '已关闭向量记忆');
    paintVecState();
    if (c.embOn) {
      (window.brainLoad().items || []).forEach(function (x) { if (!x.vec) queue(x.id); });
    }
  }

  function testEmbed() {
    var el = document.getElementById('embState');
    if (!embReady()) {
      if (el) el.textContent = '先填接口和模型 ›';
      if (window.toast) window.toast('先填 Embedding 接口、Key 和模型');
      return;
    }
    if (el) el.textContent = '测试中…';
    embedText('海边的风', false).then(function (v) {
      if (el) el.textContent = '可用 · ' + v.length + ' 维';
      if (window.toast) window.toast('向量接口可用 · ' + v.length + ' 维');
      paintVecState();
    }).catch(function (e) {
      if (el) el.textContent = '失败 ›';
      if (window.toast) window.toast('接口失败 · ' + e.message);
    });
  }

  function memRebuildVectors() {
    if (!embReady()) {
      if (window.toast) window.toast('先填 Embedding 接口和模型');
      return;
    }
    var items = window.brainLoad().items || [];
    if (!confirm('重新生成 ' + items.length + ' 条记忆的向量？旧向量会被覆盖。')) return;
    items.forEach(function (x) { delete x.vec; delete x.vecAt; delete x.vecModel; });
    TRIED = {};
    VEC_CACHE.clear();
    dbPutAll(items).then(function () {
      items.forEach(function (x) { queue(x.id); });
      if (window.toast) window.toast('开始重建 · ' + items.length + ' 条');
      paintVecState();
    });
  }

  function memInitUI() {
    var map = { embUrl: 'embUrl', embKey: 'embKey', embModel: 'embModel', embDims: 'embDims' };
    Object.keys(map).forEach(function (id) {
      var el = document.getElementById(id);
      if (!el) return;
      var k = map[id];
      el.value = cfgObj()[k] || '';
      el.addEventListener('change', function () {
        var v = el.value.trim();
        cfgObj()[k] = (k === 'embDims') ? (Number(v) || '') : v;
        if (window.saveCfg) window.saveCfg();
        VEC_CACHE.clear();
        paintVecState();
      });
    });
    var sw = document.getElementById('embOn');
    if (sw) sw.classList.toggle('on', !!cfgObj().embOn);
    paintVecState();
  }

  /* 暴露给内联脚本与 onclick */
  window.memBoot = memBoot;
  window.memOnSave = memOnSave;
  window.memRecallBlock = memRecallBlock;
  window.memInitUI = memInitUI;
  window.toggleEmb = toggleEmb;
  window.testEmbed = testEmbed;
  window.memRebuildVectors = memRebuildVectors;
  window.paintVecState = paintVecState;
})();
