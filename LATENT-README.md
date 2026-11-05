# Latent 记忆服务 · 使用说明

一套给多个客户端共用的记忆：Daddy（本网页）、Operit、RikkaHub 读写的都是同一份。

## 一、先启动服务（Termux）

```bash
cd ~/storage/downloads/Daddy
bash start-latent.sh
```

第一次用要先装 Python：

```bash
pkg install python
```

看到 `Latent memory server on http://127.0.0.1:8765` 就是起来了。保持 Termux 窗口开着（或按 Termux 的通知锁住后台）。

数据在 `~/latent-memory/`：`records.json`（记忆）、`unresolved.json`（未解决）、`threads.json`（线索）、`persona.md`（人格）。整个目录复制走就是备份。

## 二、Daddy 里连接

1. 打开「记忆」页。
2. 点右上角「连接」，填 `http://127.0.0.1:8765`。
3. 状态变绿、显示「已连接」即可。

连不上时会自动退回本地模式，记忆照常能看能写，只是不同步给别的 App。

## 三、Operit / RikkaHub 接入

在各自 App 的 MCP 设置里添加一个 **Streamable HTTP** 服务器：

- 名称：`latent-memory`（随意）
- URL：`http://127.0.0.1:8765/mcp`

它们会看到八个工具：

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

一条记忆的写回格式：

```json
{
  "text": "她怕冷，空调总开 26 度。",
  "current_state": "还是这样。",
  "indexEvidence": ["原话里的一小段"]
}
```

`current_state` 是「现在是什么状态」——后来变了就再写一条新的、用 `supersede` 把旧的接上，旧的不删。

**建议在 Operit / RikkaHub 的助手人设里加一句**：

> 开始新对话时先调用 `latent_session_start`；问到我过去的事、地点、承诺之前，先用 `latent_search` 查；聊完把值得记的事用 `latent_append` 写回，带一句「现在是什么状态」。

否则它们不会主动用这些工具。

## 四、让另一台设备也能连

手机热点/同一 Wi-Fi 下：

```bash
bash start-latent.sh --lan
```

然后在另一台设备填 `http://<这台手机的局域网 IP>:8765`。

注意：服务没有登录验证，只在自己的局域网里用，别把端口暴露到公网。

## 五、接口（非 MCP 客户端可用）

```
GET  /health          健康检查
GET  /state           全量状态
GET  /persona         人格
PUT  /persona         保存人格
POST /append          写一条
POST /supersede       取代
POST /correct         撤回
POST /search          检索
POST /unresolved      未解决
POST /thread_close    线索收尾
POST /cleanup         清理
```

## 六、已知限制

- 只实现了 MCP 的 Streamable HTTP（POST /mcp），没有 SSE 长连接端点。如果某个 App 只支持 SSE 模式，告诉我，再加一个兼容端点。
- 如果 Daddy 页面是用 https 打开的（比如 GitHub Pages / Vercel），Chrome 允许页面连 `http://127.0.0.1`；但如果某个浏览器拦混合内容，改用 http 打开页面，或把服务放到 https 后面。
- 离线时写的记忆存在本机，重新连上服务会自动补传（靠 text 去重）。
- 没有账号体系，单用户设计。
- 服务不在时，Daddy 用本地 IndexedDB；其它 App 则连不上，要等服务起来。
