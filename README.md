# Daddy / Caeruleus

单文件 AI 陪伴聊天前端 + 本地共享记忆服务。跑在手机（Termux）上，浏览器打开即用，数据不出本机。

## 结构

| 文件 | 作用 |
|---|---|
| `index.html` | 整个前端（聊天 / 记忆页 / 设置 / TTS / 导入导出） |
| `latent-server.py` | 本地服务：静态页面 + 模型代理 + 记忆库（SQLite）+ MCP + 定时备份，零第三方依赖 |
| `mem-latent.js` | 前端访问记忆库的客户端（离线队列、增删改查） |
| `mem-local.js` | 无服务端时的本地兜底记忆 |
| `cs.sh` | 一键启动：oc 代理（9355）+ 页面与记忆服务（8765） |
| `start-latent.sh` | 只启动页面与记忆服务 |
| `sw.js` / `manifest.webmanifest` | PWA |
| `LATENT-README.md` | 记忆服务的接口与数据模型说明 |

## 启动

```bash
cd ~/caeruleus
bash start-latent.sh          # 或 bash cs.sh
```

首次启动会自动生成访问口令，并打印配对链接：

```
配对链接（手机上打开一次，页面和书签都会记住）：http://127.0.0.1:8765/?t=<token>
```

在浏览器里打开一次即可，之后直接访问 `http://127.0.0.1:8765/`。
口令存在 `<数据目录>/token`（权限 600），也可以用 `--token` / `--no-token` 指定或关闭。

数据目录默认 `~/latent-memory`，含 `latent.db` 与 `backups/`。

## 数据与备份

- 所有 `/memory/*`、`/persona`、`/mcp`、`/api/*` 都要口令；`/api/*` 只认 `X-Latent-Token`，不会把上游模型的 `Authorization` 当口令。
- 静态目录只放出页面需要的文件，`.py` / `.sh` / `.md` / `.db` / 备份目录一律不可下载。
- 备份用 `VACUUM INTO` 做一致性快照，保留最近 14 份；后台每 6 小时检查一次。记忆页有「立即备份 / 下载」按钮，也可以：

```bash
curl -H "X-Latent-Token: $TOKEN" http://127.0.0.1:8765/memory/backups
curl -H "X-Latent-Token: $TOKEN" -o backup.db \
  "http://127.0.0.1:8765/memory/backup?name=latent-20261106-045400.db"
```

## 说明

- 模型 Key、TTS Key 只存在浏览器 localStorage，不进代码、不进仓库。
- `DEFAULT_BASE` 默认走本机 `127.0.0.1:8765/api`，由 `latent-server.py` 转发到上游，解决浏览器 CORS。
- 局域网访问：`python3 latent-server.py --lan`（此时口令必须开启）。
