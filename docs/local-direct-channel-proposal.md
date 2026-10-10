# 方案：桌面端 ↔ Connector 本地直连

## 1. 今天的数据流（实测）

提交一条消息：

```
客户端(桌面/Web/Android) --HTTPS--> closex.cc
  --> 服务端先读一次 runtime 状态（connector WebSocket RPC `session.state`，10s 预算）
  --> 服务端再发 `session.send_message`（同一条 WebSocket）
  --> Connector --loopback TCP JSON-RPC--> DSH 插件 --> DSH 运行时
```

运行时的活动回传：

```
DSH --> 插件 SyncFeed --loopback--> Connector SyncRelay
  --> HTTPS POST /api/v2/connector/ingest（或小消息走 WebSocket）
  --> 服务端 TimelineBroker --WebSocket--> 客户端
```

除了 `Connector ↔ 插件` 这一段，**每一跳都是公网**。本机三件东西（客户端、Connector、运行时）明明在同一台机器上，消息却要绕一圈出境再回来。

### 已经存在的东西（可直接复用）

| 能力 | 位置 | 说明 |
|---|---|---|
| 插件 ↔ Connector 的本地通道 | `dsh-bridge-next/src/host/dsh-runtime/server.ts:59-78` | `127.0.0.1:0` 临时端口 + 32 字节 token + `~/.dsh/agents-anywhere/bridge/endpoint.json`（`0600`），10s 鉴权期限，最多 16 连接 |
| Connector 侧的本地客户端 | `connector/connector/runtimes/dsh/bridge/client.py:84-91` | 已实现，可直接照抄 |
| 客户端 ↔ Connector 的**控制**通道 | `connector/connector/core/json_rpc.py:131` + `cli.py:107-109` | **stdio** JSON-RPC，只有 `connector.getState/saveConfig/start/stop/startPairing` 等控制面方法，**不含任何会话/时间线数据** |
| 桌面端已有的请求代理 | `desktop-workbench/electron/api-proxy.ts:25-30` | 目前只是把请求转发到远端，为了绕 CORS；是接入本地通道的天然位置 |

### 明确不存在的东西

- 客户端**从不**连接本机 Connector 取数据：`desktop-next/`、`web-next/src`、`desktop-workbench` 全文搜索没有任何 connector 本地端点。
- Connector 没有任何 HTTP/WS/Unix socket 数据面服务。
- `session.send_message` 只能经服务端下发（服务端还会先检查 connector 在线并读一次运行时状态）。
- `docs/` 里没有任何本地直连/本地优先的设计。

---

## 2. 推荐方案：本地**快车道**，服务端仍是唯一权威

一句话：**Connector 开一个带令牌的 loopback 数据面，桌面端优先走它；服务端继续做持久化与多端同步的唯一真相。**

这不是"绕过服务器"，而是把"本机产生、本机消费"的那部分流量从公网拿回来。

### 2.1 Connector：新增本地数据面服务

复用插件 bridge 已经验证过的模式（临时端口 + token + `0600` 描述符 + 鉴权期限）：

- 监听 `127.0.0.1:0`，`randomBytes(32)` token，描述符写到
  `~/Library/Application Support/Agents Anywhere/connector/local-endpoint.json`（`0600`）。
- 协议：JSON-RPC over WebSocket（与插件 bridge 同构，Connector 侧代码可大量复用
  `runtimes/dsh/bridge/client.py` 的对端实现）。
- 鉴权：首个请求必须是 `initialize` + token（`timingSafeEqual`），否则 10s 内断开。

**只读方法（第一阶段）**

| 方法 | 作用 | 数据来源 |
|---|---|---|
| `session.list` | 本机会话清单 | Connector 已经维护的 runtime 会话表 |
| `session.timeline` | `after=<seq>` 增量取时间线 | Connector 已经收到的 `timeline.itemUpsert` / `timeline.sync` |
| `session.state` | 当前状态 | 同名的现有实现 |
| `session.subscribe` | 订阅某会话的实时增量 | Connector 收到什么就转发什么 |

**关键点：这些数据 Connector 本来就已经有了**，不需要新增采集，只是换一条路送出去。

**写方法（第二阶段）**

| 方法 | 作用 |
|---|---|
| `session.sendMessage` | 直接交给运行时，同时把这次提交报告给服务端 |

写路径要保证"服务端最终知道这条消息"：Connector 在把消息交给运行时之后，
必须把同一条消息（带 `clientMessageId`）作为 notification 上报服务端，服务端负责去重
（`clientMessageId` 已经是现有协议的一部分，见 `api/sessions.py` 的消息接口）。

### 2.2 桌面端：优先本地，失败自动回落

在 `desktop-workbench/electron/api-proxy.ts` 这一层做：

```
如果是本机 Connector 覆盖的方法（session.timeline / session.subscribe / session.sendMessage）
  且 本地端点可用 且 token 有效
  → 走本地
否则
  → 走远端（今天的行为，不变）
```

**必须保留远端回落**：Connector 没启动、描述符过期、token 失效、用户在多台设备上 —— 任何一种情况都必须无感退回远端。

### 2.3 一致性规则（必须写死，否则会出现"两边不一样"）

1. **服务端是权威**：本地时间线是缓存，`seq` 以服务端为准；本地数据带 `source: "local"` 标记。
2. **本地只负责"更快看到"**，不负责"只有这里能看到"：任何本地展示过的条目，最终必须能在服务端查到。
3. **去重靠既有主键**：`clientMessageId`（发送）与 `timeline item id + revision`（接收）。
4. **断线时丢弃本地缓存**，重新从服务端拉一次基线 —— 不要试图在本地做冲突合并。

### 2.4 收益（预期）

| 场景 | 今天 | 本地直连后 |
|---|---|---|
| 打开会话看到最新活动 | 公网 RTT ×2（客户端↔服务端↔Connector） | loopback，≈0 |
| 发送消息到运行时开始工作 | 客户端→服务端→Connector→运行时（且过 `session.state` 闸门） | 客户端→Connector→运行时 |
| 弱网/代理抖动 | 直接影响交互 | 只影响后台同步 |

### 2.5 风险与代价

- **这是一条新的本地攻击面**：必须 token 鉴权 + `0600` 描述符 + 鉴权超时 + 只监听 `127.0.0.1`。插件 bridge 的实现可以直接照搬，不要自己发明。
- **要同时改 Connector 和桌面端**，且客户端改动需要重新构建安装包才能到用户手上（见 §3）。
- **多用户/多设备语义**：本地快车道只对"这台机器的这个用户"成立，其他设备仍然走远端；不要让它变成第二套真相。
- 建议**分两期**：先只读（风险低、收益已经很大），确认稳定后再做写路径。

---

## 3. 交付路径（重要）

这决定了改动多久能到你手上：

| 改动 | 交付方式 |
|---|---|
| `connector/**`（本次延迟修复、gzip） | 打包进 Desktop 应用（`desktop-workbench/package.json:58-68` 的 `extraResources`），**必须重新构建并安装 DMG**；或走 `devtools/release.sh` 发连接器包 |
| `server/**`（gzip 解压、健康端点能力声明） | 需要**部署服务端镜像**（`docs/deployment-hosts.md:72-89`，三步手工流程） |

> 注意：仓库里 `docs/deployment-hosts.md:11` 提交了生产机的 root 明文密码，建议尽快轮换并从文件和历史中移除。

---

## 4. 建议的排期

1. **已完成**：延迟与传输量优化（状态读取解耦、同步 ACK 解耦、条目合并、gzip 协商）。
2. **下一步（本次）**：本文档评审，确认是否做本地直连、以及是否先只读。
3. **只读快车道**：Connector 本地服务 + 桌面端代理接入。
4. **写快车道**：`session.sendMessage` + 服务端去重。
