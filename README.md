# Daddy

专属陪伴聊天前端（OpenCode Go 供应商）。单文件静态站点。

**打开：** https://liuyunyun1hao.github.io/daddy/

## 为什么需要一个代理

OpenCode Go 的 `/chat/completions` 和 `/usage` 不返回 CORS 头（`OPTIONS` 预检直接 404），
浏览器从 GitHub Pages 直连一定会被拦。所以页面所有 `/api/*` 请求都打到一个同源代理上，
由代理转发到 `https://opencode.ai/zen/go/v1/*` 并补上 CORS 头。

`proxy-server.py` 就是那个代理（零依赖，只用标准库 + 系统 curl）：

```bash
python3 proxy-server.py 8787        # 然后浏览器打开 http://127.0.0.1:8787/
```

## 内置代理地址

页面里的 `DEFAULT_BASE` 指向一个临时隧道（本机跑着代理 + SSH 反向隧道）。
隧道会断，断了之后：**设置 → 模型 → 接口地址** 填新的代理地址即可，不用重新部署。

想彻底稳定：把 `proxy-server.py` 放到一台常开的机器上，把它的地址填进「接口地址」。

## Key

API Key 只存在浏览器 localStorage，不进代码、不进仓库。
