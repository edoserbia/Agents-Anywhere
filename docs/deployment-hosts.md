# 部署主机与设备

最后核对：**2026-11-15**。本页记录非敏感运维信息；SSH 密码、账号密码、Connector token、签名密钥和 `.env` 内容不得写入仓库。

## 当前生产主机

| 项目 | 值 |
| --- | --- |
| 用途 | Agents Anywhere 生产中转服务、API、Web 控制台、客户端下载 |
| 地址 | `116.204.134.150` |
| 域名 | `closex.cc`，2026-11-15 实测 A 记录指向该 IP |
| SSH | `ssh root@116.204.134.150`；凭据由维护者独立保管 |
| API 与控制台 | `https://closex.cc`，API namespace `/api/v2` |
| 客户端下载页 | `https://closex.cc/download/` |
| 运维账号 | `edoserbia@163.com`；密码不记录在项目中 |
| 系统与资源 | Ubuntu Linux x86_64，约 2 GB 内存、20 GB 根磁盘、2 GB swap |
| 部署目录 | `/opt/agents-anywhere` |
| Compose 文件 | `/opt/agents-anywhere/docker/docker-compose.postgres.yml` |
| Compose 项目 | `agents-anywhere` |
| 环境文件 | `/opt/agents-anywhere/.env`（保持权限 `600`） |
| 下载目录 | `/opt/aa-downloads` |
| 下载服务 | `aa-downloads.service`，脚本 `/opt/aa-downloads-server.py`，支持 HTTP Range |
| 完整迁移备份 | `/root/mig/aa-next.dump` 和 `/root/mig/aa-next.dump.enc`；包含敏感数据，不得公开 |

**客户端和 Connector 一律填写 `https://closex.cc`，不填写服务器 IP。** 域名迁移已保留数据库及签名密钥，已有配对无需重建；只有账号会话过期时才需要重新登录。

### 服务与端口

| 端口 / 容器 | 用途 |
| --- | --- |
| `80` / `443`，`sub2api-caddy` | Caddy TLS 与路由；同机其他业务也依赖它 |
| `4000`，`agents-anywhere-server-next-1` | API 与静态 Web；容器内 `8000` |
| `4001`，`aa-downloads.service` | 安装包静态服务，通过域名 `/download/` 访问 |
| `agents-anywhere-postgres-next-1` | PostgreSQL；`5432` 仅容器内部 |
| `agents-anywhere-redis-next-1` | Redis；`6379` 仅容器内部 |

当前容器内存上限：Server `1600m`、PostgreSQL `700m`、Redis `256m`、迁移任务 `512m`。这是 2026-11-15 的现状记录，**不是可同时占满的内存预算**；本机还承载其他服务，变更前检查实际内存与 swap，不要照搬历史主机的限制。

### 同机服务与 Caddy 约束

`sub2api-caddy`、`vpn-sing-box.service`、`vpn-subscription.service` 和 MeshVPN 等为既有业务。更新 Agents Anywhere 时不得删除其单元文件、数据或路由，不得重启整套 Caddy 容器。

Caddy 主配置：`/root/apps/tokenoffer/deploy/Caddyfile`（容器 bind mount）。如确需变更：

```bash
cp /root/apps/tokenoffer/deploy/Caddyfile /root/apps/tokenoffer/deploy/Caddyfile.bak-$(date +%Y%m%d-%H%M%S)
docker cp /root/apps/tokenoffer/deploy/Caddyfile sub2api-caddy:/tmp/aa-caddy-check
docker exec sub2api-caddy caddy validate --config /tmp/aa-caddy-check --adapter caddyfile
docker exec sub2api-caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
```

保留现有 `/vpn/*` 路由及兼容重写。容器中的代理地址应使用**实际 Docker 网络网关**，不能把 `127.0.0.1` 当作宿主机。

`/download` 必须 301 重定向到 `/download/`，否则相对安装包链接会被解析到根目录，可能下载到返回 200 的 Web HTML，而不是真正的安装包。验收应跟随最终 URL、核对文件大小和 SHA256；不能只检查 HTTP 200。

### 环境和数据库约束

- 保留 `POSTGRES_PASSWORD` 与 `AGENT_SERVER_SECRET`，不要因迁移或重新登录而更换。
- `AGENT_SERVER_PUBLIC_ORIGIN=https://closex.cc`；CORS 根据实际客户端配置，不把密码写入 URL。
- 不使用 `--reset-data`、不删除 Compose volumes，也不把生产数据库 URL 带入本地测试进程。
- 新主机刚迁入时，磁盘上的部署源码落后于运行容器。**运行中的容器才是当时的生产代码基线**：数据库包含 `v2_44_jupiter`，直接用旧 `/opt/agents-anywhere/server` 构建可能缺迁移文件。
- 部署新 Server 前先对照运行容器、完整源码和数据库 revision。不要重跑旧数据库导入或覆盖增量会话。

## 工作设备

三台设备在 2026-11-15 已确认使用域名且在线。各自的 Connector ID、Runtime 配置和历史保留；不把多台设备绑定成同一个 ID。

| 设备 | SSH | 类型与服务 | 当前生效的配对配置 |
| --- | --- | --- | --- |
| `underground` | `ssh mac@192.168.124.31` | Intel macOS，`/Applications/Agents Anywhere.app` 2.1.2，桌面内置 Connector | `~/Library/Application Support/Agents Anywhere/connector/connector.json` |
| `2080ti-server` | `ssh jupiter@10.66.0.20` | Linux CLI 2.1.2，systemd `anywhere-cli.service` | `~/.agents-anywhere/connector.json` |
| `jupiter-B365M-POWER` | `ssh jupiter@192.168.124.9` | Linux CLI **2.1.0（兼容性保留）**，systemd `anywhere-connector.service`；另有用户级 `dsh-desktop.service` | `~/.agents-anywhere/dsh-bridge-next/connector/connector.json` |

注意：B365 上 `~/.agents-anywhere/connector.json` 是旧副本，不是服务实际使用的配置。2080ti 上还留有旧桌面客户端配置，不应启动另一个内置 Connector 与 systemd 服务争用身份。

### Linux 更新与回滚

新版源码和独立虚拟环境放在 `~/.agents-anywhere/releases/connector-2.1.2/`。安装成功后再通过 systemd drop-in 切换 `ExecStart`，继续使用上表中**原来的配置文件**。原环境保留，便于移除 drop-in 后回滚，不在运行中直接覆盖旧 venv。

- 2080ti 当前使用 `/etc/systemd/system/anywhere-cli.service.d/20-connector-2.1.2.conf`。
- **B365 已回滚至原 `~/.agents-anywhere/dsh-bridge-next/connector-venv/`（2.1.0）**：该机内置 DSH 插件使用 MeshCall / `dsh-bridge/connector.json`，本仓库 2.1.2 Connector 则使用 `endpoint.json` / JSON-RPC Bridge，试运行不能连接其 DSH。新版 drop-in 已撤销，副本保存为 `~/.agents-anywhere/releases/connector-2.1.2/20-connector-2.1.2.conf.disabled`。不要未核对协议就重新启用，也不要对 DSH Desktop 内置插件叠加独立 npm 安装。
- B365 的 DSH 插件账号已刷新登录到 `https://closex.cc`，账号文件及登录前备份权限均为 0600。三机配对 token 未旋转。

```bash
systemctl status anywhere-cli.service           # 2080ti
systemctl status anywhere-connector.service     # B365
# 以各自原配置、日志和 systemd 状态为准；不要输出其中的 token。
```

Connector 应直接访问 `closex.cc`。两台 Linux 主机的代理曾导致 TLS reset；保留原服务中清空代理变量或 `NO_PROXY` 的直连配置，不随手加入 HTTP(S) 代理。

更新前检查是否有运行中的任务。桌面升级需完整退出旧客户端，再打开新版，才会加载新的内置 Connector；仅替换应用文件不等于进程已更新。升级结束后同时核对域名登录、Connector 在线状态和 Runtime 列表。

### B365 DSH 恢复记录

2026-11-15 排查发现 `/dev/urandom` 缺失，DSH Electron zygote 启动时因无法打开它而崩溃。已恢复标准字符设备 `1:9`、权限 `0666`，没有关闭 Chromium sandbox。另仅清理已退出 PID 的 `SingletonLock` / `SingletonCookie` / `SingletonSocket` 符号链接，没有删除浏览器 profile 或会话。

保留用户级 `dsh-desktop.service`，新增 `~/.config/systemd/user/dsh-desktop.service.d/20-logging.conf` 将输出写入 `~/.agents-anywhere/logs/dsh-desktop-service.log`。该机 DSH Desktop Host 自身日志位于 `~/.config/DSH Desktop/logs/`，不是旧 `~/.dsh/logs/`。20:15（Asia/Shanghai）复查用户服务 active，Codex / DSH 均 running、error=null。

## 2026-11-15 连接与流量检查

- 已使用运维账号成功登录新主机的域名 API，并确认上述三台设备在线。
- 本机 `/private/tmp` 的带宽排查会话已确认：Web / Desktop 的会话队列曾每 2 秒读取一次，权限目录每 5 秒一次。
- 现有修复把队列改为 **8 秒**、权限目录改为 **30 秒**；页面隐藏时不轮询，恢复可见或窗口 focus 时刷新。Web 版已部署，macOS 需要安装新版才能生效。
- Android 原先仍有 2 秒队列轮询；2.1.2 同步改为前台 8 秒，回到前台立即读取一次。
- 本次不新增 WebSocket 队列事件，避免在兼容补丁中改变跨端协议。8 秒是兜底轮询频率，不声称已完全消除请求。

## 2026-11-15 磁盘清理

按维护者要求，仅删除确认可重建或重复的文件：

1. `/root/mig/chunk.0` 至 `chunk.7`：拼接后的 SHA256 与完整 `/root/mig/aa-next.dump.enc` 一致，删除重复传输分片（约 420 MB）。
2. `aa-web-build:latest`：确认没有容器使用后删除这个临时构建镜像，**未执行全局 Docker prune**。

清理前根分区约剩 **1.7 GB**；清理后约剩 **4.5 GB**。完整 dump、加密 dump、数据库卷、附件卷、生产镜像和其他服务均保留。该数字为上传新版安装包之前的快照；三个安装包发布后约剩 **3.9 GB**（根分区使用率 81%）。完整迁移备份权限已收紧至 `0600`。

## 定向构建与下载发布

本次用户要求的产物只有：macOS Universal DMG、Linux Connector 源码包、Android Release APK。**不构建 Windows、Linux 桌面、iOS。** 版本与签名边界见 [2.1.2 发布说明](releases/2.1.2.md)。

```bash
# 先按 PATCH 规则更新 Desktop / Renderer / Connector / Android 版本并写发布说明。
# 需要 SSH 已认证；默认发布目标为 root@closex.cc，不再依赖历史 aa-new 别名。
./devtools/release-closex.sh 2.1.2

# 已构建好的同版本产物：跳过构建，继续断点上传、验证并发布页面。
./devtools/release-closex.sh 2.1.2 --no-build
```

可用 `AA_REMOTE`、`AA_DOWNLOAD_DIR`、`AA_SERVER_URL`、`AA_PAGE_URL` 覆盖部署目标；已有 SSH ControlMaster 时可设置 `AA_SSH_CONTROL_PATH`。脚本不保存或传递密码。

- 安装包先上传到隐藏 staging 目录，支持断点续传；完整 SHA256 通过后才替换正式文件。
- 三个产物就绪后才原子替换下载首页；页面不展示未上传的平台。
- `SHA256SUMS.txt` 提供完整哈希；公开域名下载另验证 Range 内容，避免在低带宽链路重复下载整个 DMG。
- Android 使用普通非 debuggable Release APK，显式沿用原 debug 证书以允许覆盖旧版本，不是模拟器专用包或正式发行证书包。
- 2026-11-15 发布完成：三个产物完整服务端 SHA256 和公网 Range 内容均通过；大小 / 哈希见发布说明。临时 HTTPS 上传入口已撤销（PUT 501），上传 token 文件已删除，过期 timer 已停止，不保留对外写入口。

## 历史主机（不是当前生产目标）

| 地址 | 历史用途 | 注意 |
| --- | --- | --- |
| `156.225.23.215` | 2026-09-16 至本次迁移前的生产主机；本机历史 SSH 别名 `aa-new` 仍可能指向它 | 不再作为默认发布目标，不把新包误传到该机 |
| `124.220.147.199` | 更早的主机；历史别名 `aa-server` | 数据、服务存续状态需另行核实，不能直接宣称可无损回滚 |

2026-11-15 已切换域名并继续写入新主机。任何切回旧主机的操作都必须先同步新产生的数据；不能直接回滚旧数据库而丢失新会话。
