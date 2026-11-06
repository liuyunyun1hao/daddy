# Latent 记忆服务 · 使用说明

一套给多个客户端共用的记忆：Daddy（本网页）、Operit、RikkaHub 读写的都是同一份。
服务端是唯一真相源；Daddy 本地只保留缓存和待同步队列，所以多窗口、多设备、多次开新会话都继承同一份记忆。

## 一、先启动服务（Termux）

```bash
cd ~/storage/downloads/Daddy
bash start-latent.sh
```

第一次用要先装 Python：

```bash
pkg install python
```

如果 `cd ~/storage/downloads/Daddy` 提示目录不存在，先在 Termux 里执行一次 `termux-setup-storage`，允许文件访问。

看到 `Latent memory server v2 on http://127.0.0.1:8765` 就是起来了。保持 Termux 窗口开着（或按 Termux 的通知锁住后台）。

数据在 `~/latent-memory/latent.db`（SQLite，WAL 模式）。同一目录下：
- `token`：访问口令，权限 0600，第一次启动自动生成。
- `backups/`：每 6 小时自动一份（`VACUUM INTO`，WAL 下也是一致快照），保留最近 14 份；记忆页可以手动备份和下载。

鉴权默认开启，而且**没有"本机放行"**：Android 上 127.0.0.1 是所有 App 共享的，本机不等于页面自己。启动日志会打印一行配对链接：

```
配对链接：http://127.0.0.1:8765/?t=<token>
```

手机上打开一次，服务端会种一个同源 cookie 并跳回干净地址；之后直接开书签、刷新都不用再贴 token。页面同时会把 token 存进 localStorage（给 MCP / 其他客户端复用）。

旧版的 `records.json` / `unresolved.json` / `threads.json` / `persona.md` 会在第一次启动时自动迁移进来。

## 二、Daddy 里连接

1. 打开「记忆」页。
2. 一般不用手动配：用启动日志里的配对链接打开一次就行。换设备或清过缓存时，点右上角「连接」，地址填 `http://127.0.0.1:8765`，token 填 `~/latent-memory/token` 里的内容。
3. 状态变绿、显示「已连接 · rev N」即可。

连不上时会显示「离线 · 待同步 N」，记忆照常能看能写；恢复连接后按 `op_id` 幂等补传，不会丢。

## 三、Operit / RikkaHub 接入

在各自 App 的 MCP 设置里添加一个 **Streamable HTTP** 服务器：

- 名称：`latent-memory`（随意）
- URL：`http://127.0.0.1:8765/mcp`
- 如果服务端开了 token，在请求头里带 `Authorization: Bearer <token>`

原有八个工具继续可用，另有简化工具。**所有工具都接受 `scope`**（默认 `main`；剧场用 `stage:<角色卡id>`），不传就落主线，跨客户端兼容：

| 工具 | 用途 |
| --- | --- |
| `latent_session_start` | 开新对话时调用，取回未解决的事、最近线索、人格 |
| `latent_search` | 只读检索，问过去的事之前先查 |
| `latent_append` | 写一条记忆，必须带 text，建议带 current_state |
| `latent_supersede` | 当时是真的、现在变了（保留变迁链） |
| `latent_correct` | 从来没发生过（撤回） |
| `latent_cleanup` | 删掉没用的 |
| `latent_unresolved` | 查看/更新未解决清单 |
| `latent_thread_close` | 结束一段线索，存快照 |
| `latent_remember` | 简化写入：text / kind / topic / current_state |
| `latent_recall` | 简化检索：query / k |
| `latent_resolve` | 按 id、topic 或 summary 关闭未解决 |
| `latent_forget` | 按 id / topic / about 遗忘 |
| `latent_snapshot` | 拉取某个 rev 之后的增量快照 |
| `latent_state` | 读/写「当前状态」（working memory）：`scope` + 可选 `text` |
| `latent_consolidate` | 把同 scope、同 topic 的多条旧说法合并成一条 |

**建议在 Operit / RikkaHub 的助手人设里加一句**：

> 开始新对话时先调用 `latent_session_start`；问到我过去的事、地点、承诺之前，先用 `latent_search` 查；聊完把值得记的事用 `latent_append` 写回，带一句「现在是什么状态」。

否则它们不会主动用这些工具。

## 四、让另一台设备也能连

手机热点/同一 Wi-Fi 下：

```bash
bash start-latent.sh --lan
```

然后在另一台设备打开启动日志里的配对链接（`http://<这台手机的局域网 IP>:8765/?t=<token>`）。

token 会自动生成；想自己定就加 `--token=...`（两台设备要一致）。
注意：`--lan` 会把服务暴露到局域网。公网使用请放在 Tailscale / Cloudflare Tunnel 后面，不要直接把端口映射出去。

## 五、接口（非 MCP 客户端可用）

新接口（推荐，带 rev / op_id 幂等）：

```
POST /memory/op                     批量写：{op_id, device_id, session_id, base_rev, ops:[...]}
GET  /memory/snapshot?since_rev=N   增量快照（records / unresolved / threads / states / persona，可加 &scope=）
GET  /memory/recall?q=词&k=6        检索（BM25 + 时间衰减 + weight，可加 &scope=）
GET  /memory/state?scope=main       读某场景的「当前状态」
POST /memory/state                  写「当前状态」：{scope, text, op_id}
GET  /memory/stream?since_rev=N     SSE 增量推送（服务端每 2 秒检查一次）
GET  /memory/export                 全量导出 JSON
```

op 类型：`remember` / `supersede` / `correct` / `forget` / `cleanup` / `resolve` / `unresolved` / `thread_close` / `state`（写 working memory）/ `consolidate`（合并同 scope+topic 的多条旧说法）。
写记忆时带 `topic`，同 topic 的新说法会自动把旧记录标成 `superseded`，旧记录不物理删除。

旧接口仍保留（内部转成 op）：

```
GET  /health          健康检查
GET  /state           全量状态
GET  /persona         人格
PUT  /persona         保存人格（写接口，需要 token）
POST /persona         同上（旧客户端用的就是 POST）
POST /append          写一条
POST /supersede       取代
POST /correct         撤回
POST /search          检索
POST /unresolved      未解决
POST /thread_close    线索收尾
POST /cleanup         清理
```

除 `/health` 外，所有记忆读写接口、`/persona`、`/mcp` 和 `/api/*` 模型代理都需要 token：`X-Latent-Token: <token>`（推荐）、`Authorization: Bearer <token>`（给 MCP 客户端），或配对时种下的同源 cookie。

 `/api/*` 只认 `X-Latent-Token` / cookie，不读 `Authorization`——那条头要原样转给上游模型，混用会把模型 Key 当成口令。

备份接口：`GET /memory/backups` 列表、`POST /memory/backup` 立即备份、`GET /memory/backup?name=latent-YYYYmmdd-HHMMSS.db` 下载。

## 六、已知限制

- MCP 只实现了 Streamable HTTP（POST /mcp），没有 MCP 的 SSE 长连接端点；记忆同步本身有 `/memory/stream` SSE。
- 如果 Daddy 页面是用 https 打开的（比如 GitHub Pages / Vercel），Chrome 允许页面连 `http://127.0.0.1`；但如果某个浏览器拦混合内容，改用 http 打开页面，或把服务放到 https 后面。
- 没有账号体系，单用户设计；token 只是共享口令，不是多用户隔离。
- token 明文存在数据目录的 `token` 文件里（权限 0600），和数据库一样要一起备份、一起保密。
- 静态目录只对外提供页面与前端资源；`.md` / `.py` / `.sh` / `.db`、点开头文件、`backup-*` 目录一律返回 404。
- 服务不在时，Daddy 用本地 IndexedDB 缓存继续工作，待同步队列不会因为重试次数丢弃。
