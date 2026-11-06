/* Caeruleus PWA · 页面与脚本网络优先，离线回缓存 */
const CACHE = 'caeruleus-v6';
const ASSETS = ['./', './index.html', './mem-local.js', './mem-latent.js', './sw.js', './manifest.webmanifest', './icon-192.png', './icon-512.png'];

self.addEventListener('install', function (e) {
  /* 逐个 add：addAll 只要有一个 404（换了图标/少文件）整次安装就失败，SW 永远装不上 */
  e.waitUntil(caches.open(CACHE).then(function (c) {
    return Promise.all(ASSETS.map(function (a) {
      return c.add(a).catch(function () { /* 单个资源失败不阻断安装 */ });
    }));
  }).then(function () { return self.skipWaiting(); }));
});

self.addEventListener('activate', function (e) {
  e.waitUntil(caches.keys().then(function (ks) {
    return Promise.all(ks.filter(function (k) { return k !== CACHE; }).map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

self.addEventListener('fetch', function (e) {
  const req = e.request;
  if (req.method !== 'GET') return;
  let url;
  try { url = new URL(req.url); } catch (x) { return; }
  if (url.origin !== location.origin) return;      /* 跨域（OpenCode Go / ElevenLabs / 代理）不拦 */
  if (url.pathname.indexOf('/api/') >= 0) return;  /* 代理请求直达网络 */
  /* 记忆服务是动态接口：不缓存、不离线回放，否则会读到旧记忆或把缓存撑爆 */
  if (/^\/(memory|mcp|health|state|persona|append|supersede|correct|cleanup|search|unresolved|thread_close|session_start)(\/|$)/.test(url.pathname)) return;

  if (req.mode === 'navigate') {
    e.respondWith(
      fetch(req).then(function (r) {
        const cp = r.clone();
        caches.open(CACHE).then(function (c) { c.put(req, cp); });
        return r;
      }).catch(function () {
        return caches.match(req).then(function (r) { return r || caches.match('./index.html'); });
      })
    );
    return;
  }
  /* 脚本与样式必须网络优先：否则 mem-latent.js 更新后手机永远跑旧版 */
  e.respondWith(
    fetch(req).then(function (r) {
      if (r && r.ok) {
        const cp = r.clone();
        caches.open(CACHE).then(function (c) { c.put(req, cp); });
      }
      return r;
    }).catch(function () { return caches.match(req); })
  );
});
