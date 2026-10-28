# Daddy

专属陪伴聊天前端（OpenCode Go 供应商）。

- 单文件静态站点：`index.html`
- 接口走一个同源代理（`/api/*` → `https://opencode.ai/zen/go/v1/*`），因为 OpenCode Go 的
  `/chat/completions` 与 `/usage` 没有 CORS 预检支持，浏览器直连会被拦。
- API Key 只存在浏览器 localStorage，不写进代码。

打开：https://liuyunyun1hao.github.io/daddy/
