# 2.0.26 部署手册

本手册只描述**这次改动**需要走的步骤。底层主机的既有事实（主机、端口、共租服务、
Caddy 路由）见 `docs/deployment-hosts.md`，不要照抄任何密钥到别处。

## 0. 先读这一条：两种发布物，缺一不可

服务端镜像和桌面端**必须都更新**，只做一半会有明确的后果：

- **只部署服务端**：gzip 解压、断点续传接口、健康端点能力声明生效，但 Connector 侧
  （收到即确认、流式窗口、按字节分批、重连重建连接池）**到不了用户手上**——桌面包里自带一份
  `../connector` 的副本（`desktop-workbench/package.json` 的 `extraResources`），运行时用的就是它。
- **只重建桌面端**：Connector 会探测能力声明，服务端没声明就走整包明文 JSON，功能不坏但没有压缩。

判定方法（部署前后各看一次）：确认正在运行的 Connector 到底加载了哪份代码——

```
"/Users/mac/Library/Application Support/Agents Anywhere/connector/.venv/bin/python3" \
  -c "import connector.server.ingest as m; print(m.__file__)"
```

输出指向 `Agents Anywhere.app/Contents/Resources/connector/...` 就是**包内副本**（发布版），
指向仓库路径才是开发版。2026-10-10 排查时就踩过这个：以为实机在跑改动，实际跑的是 10 月 8 日的包内副本。

## 0. 这次为什么要部署

| 改动位置 | 交付方式 | 不做的后果 |
|---|---|---|
| `connector/**` | 打进桌面端安装包（`devtools/release.sh`） | 用户机器上的延迟/卡顿问题不会消失 |
| `server/**` | 服务端镜像 | gzip 解压与断点续传接口不存在；Connector 会**自动退回整包明文**，不会报错，但弱网优化不生效 |

两者可以**分开上**：所有新能力都是服务端先声明、Connector 才使用。

## 1. 前置条件（缺一不可）

1. **改动已提交并推送**。改动已在分支
   `fix/session-transport-latency-and-weak-network`（提交 `e0e10e6e`，`main` 未动）。
   部署是在旧主机 `/tmp/aa-ctx` 的检出上构建的，所以还需要**推送该分支**并把
   `/tmp/aa-ctx` 更新到它（或先合并到 `main` 再更新，按你们的习惯）。
2. **版本号已 bump**：`devtools/release.sh <版本>` 会同时改 7 处版本号（服务端、
   Desktop、Connector、Android）并发布客户端产物。**它不会部署服务端**
   （`devtools/release.sh:374-377` 明确写了）。
3. **`/opt/agents-anywhere/.env` 保持不变**，尤其是 `POSTGRES_PASSWORD` 与
   `AGENT_SERVER_SECRET`：改动它们会让所有会话与 Connector 令牌失效。
4. **备份**：部署前做一次数据库转储。迁移是**手动步骤**（服务端启动只校验版本，
   不会自动迁移），所以迁移与启动的顺序可以自己控制。

## 2. 服务端部署

严格照 `docs/deployment-hosts.md` 的三步：

```bash
# 1. 在旧主机构建（新主机拉不到 Docker Hub 的基础镜像）
ssh aa-server 'cd /tmp/aa-ctx && sudo docker build \
  --build-arg GIT_MIRROR=https://ghproxy.net/https://github.com/ \
  --build-arg APT_MIRROR=https://mirrors.tencent.com/debian \
  --build-arg PIP_INDEX_URL=https://mirrors.tencent.com/pypi/simple \
  --build-arg YARN_REGISTRY=https://registry.npmmirror.com \
  -f docker/Dockerfile -t agents-anywhere-server:release-<版本> .'

# 2. 传到新主机
ssh aa-server 'sudo docker save agents-anywhere-server:release-<版本> | gzip -1' \
  | ssh aa-new 'gunzip | docker load'

# 3. 切换镜像并重建（--no-build 是必须的：migrate-next 有 build 段）
ssh aa-new 'cd /opt/agents-anywhere
  sed -i "s|image: agents-anywhere-server:.*|image: agents-anywhere-server:release-<版本>|g" docker/docker-compose.postgres.yml
  docker compose --env-file .env -f docker/docker-compose.postgres.yml -p agents-anywhere up -d --no-build --force-recreate migrate-next server-next'
```

### 绝对不要动

- `vpn-sing-box.service` / `vpn-subscription.service`（改错会断 VPN）
- `sub2api-caddy` 容器（它占着 80/443）：Caddy 配置只能 `caddy reload`，
  **不要 restart 容器**
- `sub2api`

## 3. 服务端验收（部署后立刻做）

```bash
# 1. 版本与能力声明
curl -s https://closex.cc/api/v2/health
# 期望：version 为新版本，且出现
#   "ingest": {"encodings": ["deflate", "gzip"], "resumable": true}

# 2. 就绪与库版本
curl -s https://closex.cc/api/v2/health/ready
# 期望：status=ready，database.schemaVersion 与本次迁移后一致

# 3. 新路由存在（未带令牌应为 401，而不是 404）
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://closex.cc/api/v2/connector/ingest/uploads

# 4. 日志无异常
ssh aa-new 'cd /opt/agents-anywhere && docker compose --env-file .env -f docker/docker-compose.postgres.yml -p agents-anywhere logs --since 10m server-next | tail -50'
```

第 1 步是关键：**Connector 只有在看到 `encodings`/`resumable` 时才会启用压缩与续传**，
所以这一行同时验证了「镜像换了」和「新能力生效」。

## 3.5 部署前已经验证过的（本次实测）

在同机把**真实服务端跑在真实 uvicorn 上**、用**真实 Connector 客户端走真实 TCP** 验证过：

| 验证项 | 结果 |
|---|---|
| `GET /api/v2/health` 能力声明 | `encodings=[deflate,gzip]`、`resumable=true` |
| 明文 ingest（真实令牌） | 200，会话标题已按通知更新 |
| gzip ingest（真实令牌） | 与明文同样的授权判定，落库效果一致 |
| **chunked 传输编码** 的 gzip 请求 | 正确处理（请求体跨多条 ASGI 消息） |
| 断点续传：创建 → 6 次分片 PUT → commit | 内容完整落到时间线 |
| 暂存体编码头 `X-Upload-Encoding: gzip` | 客户端确实发送、服务端确实读取 |
| 5 条新路由 | 均返回 401（存在），未知路径返回 404 |
| 半截快照提交 | 422 拒绝，时间线未被改动 |

因此第 3 步的验收只需要确认**部署后的 closex.cc 也满足同样几条**。

## 4. 客户端与 Connector

Connector 随桌面端打包，所以必须重新构建安装包并让用户升级：

```bash
devtools/release.sh <版本>       # bump 版本 + 构建并上传各端产物
yarn dist:mac                    # 在 desktop-workbench 下构建 DMG
```

> 只更新桌面端不更新服务端：新 Connector 会探测到服务端没有声明能力，自动走整包明文，
> 不会出错。
> 只更新服务端不更新桌面端：老 Connector 照旧整包明文，同样不会出错。

## 5. 上线后观察

Connector 日志现在会直接给出链路实测吞吐（以前只能从行为反推）：

```
connector ingest upload notifications=… wire_bytes=… raw_bytes=… encoding=gzip
  elapsed_ms=… kbps=… queued=… durable=… batches=…
```

以及 DSH 同步的健康度（修复前约每分钟失败一次）：

```
DSH event sync interrupted; resubscribing for history calibration (…)
```

## 6. 回滚

服务端回滚只需把 compose 里的镜像标签改回上一个 `release-<版本>` 并重建
`server-next`（**不要**回滚 `migrate-next`：本次没有新增迁移，库版本不变）。

```bash
ssh aa-new 'cd /opt/agents-anywhere
  sed -i "s|image: agents-anywhere-server:.*|image: agents-anywhere-server:release-<上一个版本>|g" docker/docker-compose.postgres.yml
  docker compose --env-file .env -f docker/docker-compose.postgres.yml -p agents-anywhere up -d --no-build --force-recreate server-next'
```

客户端回滚即安装上一个版本的安装包。
