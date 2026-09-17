# 部署主机

Agents Anywhere 的两台主机，以及新主机上与既有服务共存的约束。

## 生产主机（新）

| 项目 | 值 |
| --- | --- |
| 地址 | `156.225.23.215` |
| 域名 | `closex.cc`（A 记录已指向本机） |
| 登录 | `ssh root@156.225.23.215`，密码 `5uqdxSatJBGH` |
| 免密钥 | 已配置：`ssh aa-new` 直接登录（`~/.ssh/config` 中 `Host aa-new`） |
| 系统 | Ubuntu 22.04 LTS，2 vCPU / 1.9 GB 内存 / 29 GB 磁盘 |
| API 与控制台 | `https://closex.cc`（Caddy 终止 TLS） |
| 客户端下载页 | `http://closex.cc:4001` |
| 部署目录 | `/opt/agents-anywhere` |
| 下载目录 | `/opt/aa-downloads` |

### 服务端口

| 端口 | 用途 |
| --- | --- |
| 4000 | Agents Anywhere API 与 Web 控制台（容器 `8000` 映射到主机 `4000`，与旧主机一致） |
| 4001 | 客户端下载页（systemd `aa-downloads.service`） |
| 5432 / 6379 | PostgreSQL 与 Redis，**仅容器网络内可达，不对主机暴露** |

### 本机已有的其他服务（请勿删改）

新主机并非空机，部署 Agents Anywhere 时必须与下列服务共存：

| 服务 | 说明 | 端口 |
| --- | --- | --- |
| `vpn-sing-box.service` | VPN（VLESS），`closex.cc` 的 `/vpn/ws/...` 路径 | `172.18.0.1:10000` |
| `vpn-subscription.service` | Clash 订阅合并服务，`/vpn/sub/...` 路径 | `172.18.0.1:8790` |
| `sub2api-caddy`（容器） | Caddy 反向代理，承载 TLS 与上述路由 | `80`、`443` |
| `sub2api` 等（容器） | 另一套 Web 服务 | 容器内部 |

**约束**：这些服务的单元文件、容器与数据都不要改动。Agents Anywhere 只使用 4000 与 4001。

### Caddy 路由

配置文件 `/root/apps/tokenoffer/deploy/Caddyfile` 由 `sub2api-caddy` 容器 bind-mount 使用。

改动前请先备份，并用 Caddy 自身校验后再热加载（**不要重启容器**，重启会中断 VPN）：

```bash
ssh aa-new
cp /root/apps/tokenoffer/deploy/Caddyfile /root/apps/tokenoffer/deploy/Caddyfile.bak-$(date +%Y%m%d-%H%M%S)
# 编辑后校验
docker cp /root/apps/tokenoffer/deploy/Caddyfile sub2api-caddy:/tmp/cf
docker exec sub2api-caddy caddy validate --config /tmp/cf --adapter caddyfile
# 热加载（不重启容器）
docker exec sub2api-caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
# 确认 VPN 未受影响
curl -s -o /dev/null -w '%{http_code}\n' "https://closex.cc/vpn/sub/<原有路径>"
```

当前配置中，`/vpn/*` 由原有 matcher 处理（**顺序与内容未改动**），其余路径（catch-all）反向代理到 Agents Anywhere：

```
handle {
    reverse_proxy 172.18.0.1:4000
}
```

注意反向代理目标是 **`172.18.0.1`（主机在容器网络中的网关）**，不是 `127.0.0.1`——容器内的 loopback 指向容器自身，用 `127.0.0.1` 会得到 502。

### 部署步骤

服务端镜像在旧主机上构建（新主机无法访问 Docker Hub，拉不到 `node:22-bookworm-slim` 等基础镜像），再传到新主机：

```bash
# 1. 在旧主机构建（build context 位于 /tmp/aa-ctx）
ssh aa-server 'cd /tmp/aa-ctx && sudo docker build \
  --build-arg GIT_MIRROR=https://ghproxy.net/https://github.com/ \
  --build-arg APT_MIRROR=https://mirrors.tencent.com/debian \
  --build-arg PIP_INDEX_URL=https://mirrors.tencent.com/pypi/simple \
  --build-arg YARN_REGISTRY=https://registry.npmmirror.com \
  -f docker/Dockerfile -t agents-anywhere-server:release-<版本> .'

# 2. 传到新主机
ssh aa-server 'sudo docker save agents-anywhere-server:release-<版本> | gzip -1' \
  | ssh aa-new 'gunzip | docker load'

# 3. 在新主机切换镜像并重建
ssh aa-new 'cd /opt/agents-anywhere
  sed -i "s|image: agents-anywhere-server:.*|image: agents-anywhere-server:release-<版本>|g" docker/docker-compose.postgres.yml
  docker compose --env-file .env -f docker/docker-compose.postgres.yml -p agents-anywhere up -d --no-build --force-recreate migrate-next server-next'
```

### 环境变量

`/opt/agents-anywhere/.env`（权限 `600`）：

| 变量 | 说明 |
| --- | --- |
| `POSTGRES_PASSWORD` | 数据库密码。**必须与旧主机一致**，否则迁移过来的数据无法访问。 |
| `AGENT_SERVER_SECRET` | 令牌签名密钥。**必须与旧主机一致**，否则所有已登录设备与连接器令牌立即失效。 |
| `AGENTS_ANYWHERE_WEB_PORT` | 主机映射端口，`4000`。 |
| `AGENT_SERVER_PUBLIC_ORIGIN` | `https://closex.cc`，用于生成对外链接。 |
| `AGENT_SERVER_CORS_ORIGINS` | 允许的来源，含域名与直连 IP。 |

## 旧主机（回滚目标）

| 项目 | 值 |
| --- | --- |
| 地址 | `124.220.147.199`（`ssh aa-server`） |
| 部署目录 | `/opt/agents-anywhere` |
| 构建上下文 | `/tmp/aa-ctx`（含绕过 GitHub 访问的 git mirror 参数） |

旧主机仍保留原有部署与数据，可随时回滚。**注意**：迁移后两边数据会各自累积，回滚会丢失迁移之后在新主机产生的会话。

## 资源限制与加固

新主机只有 1.9 GB 内存，且与 VPN 共用。以下限制让 Agents Anywhere 出问题时**只影响自己**——否则宿主机 OOM killer 会挑选占用最大的进程杀掉，可能命中 VPN。

### 容器内存上限

`docker/docker-compose.postgres.yml` 中为 AA 的三个容器设置了 `mem_limit`（同时设置 `memswap_limit`，避免用 swap 绕过限制）：

| 容器 | 上限 | 依据 |
| --- | --- | --- |
| `server-next` | 900m | 实测稳定约 287 MB（2 个 event worker），留约 3 倍余量 |
| `postgres-next` | 700m | 实测约 113 MB，其中大部分是可回收文件缓存；数据库 427 MB |
| `redis-next` | 256m | `maxmemory` 为 192 MB，上限略高于它 |
| `migrate-next` | 512m | 仅迁移时运行 |

超出上限时 Docker 只杀该容器（`restart: unless-stopped` 会自动拉起），VPN 不受影响。

### journald

`/etc/systemd/journald.conf` 原本**没有任何容量上限**，日志已涨到 2.7 GB，`systemd-journald` 进程占用 152 MB 内存。主要来源不是 AA（24 小时仅 1 行），而是：

| 来源 | 24h 行数 |
| --- | --- |
| sing-box（VPN） | 65,448 |
| sshd | 25,245（其中 6,283 次密码失败、3,231 次非法用户） |

现配置：`SystemMaxUse=500M`、`RuntimeMaxUse=64M`、`MaxRetentionSec=30day`，并启用限流。生效后 journald 内存降到约 52 MB，磁盘占用 2.7 GB → 465 MB。

### Docker 日志轮转

`/etc/docker/daemon.json` 设置 `max-size=20m`、`max-file=3`。注意：

- `systemctl reload docker` **不会**应用 `log-opts`，必须 `systemctl restart docker`。
- 所有容器都是 `unless-stopped`，重启 Docker 后会自动拉起；sing-box 是 systemd 服务，不受影响。
- 已存在的容器保留旧日志配置，需重建才生效。

改动前 `sub2api-caddy` 的日志已达 87 MB（约 7.6 MB/天）。清理时用 `truncate -s 0`，**不要删文件**——容器按 inode 持有该文件，删除后空间要等重启才释放。

### SSH 防护

`fail2ban`（`jail.local`，`sshd` jail，`mode = aggressive`，`maxretry = 5`，`bantime = 1h`）。日志显示主机持续遭受 SSH 爆破。

**`ignoreip` 只保留 loopback**，不列出任何运维地址：

```
ignoreip = 127.0.0.1/8 ::1
```

这样所有 IP 一视同仁，封禁判断不依赖运维方从哪个网络接入。因为本机使用**密钥登录**（`ssh -v` 可确认 `Authenticated to ... using "publickey"`），正常操作不会产生密码失败，也就不会被封。

### 如果被误封

出口 IP 没有白名单，所以下列情况可能触发封禁：

- 人为用密码登录并连续输错 5 次；
- 所在网络出口 IP 与攻击源重合（NAT / 共享出口）。

被封后按 1 小时自动解封，或从控制台 VNC 进入执行：

```bash
fail2ban-client set sshd unbanip <你的IP>
fail2ban-client status sshd          # 查看当前封禁列表
```

注意 `ignoreip` 是**豁免名单**（名单内的 IP 永不被封），不是访问控制——它不限制任何人访问。若确实需要免封，把地址加回该行即可。

### 临时关闭

需要完全停掉防护时：

```bash
systemctl stop fail2ban        # 立即停止（已封禁的 IP 会一并解封）
systemctl disable fail2ban     # 可选：取消开机自启
```

### 加固后的实测结果

| 指标 | 加固前 | 加固后 |
| --- | --- | --- |
| journald 内存 | 152 MB | 52 MB |
| journald 磁盘 | 2.7 GB | 465 MB |
| 根分区占用 | 14 GB (48%) | 12 GB (40%) |
| 容器内存上限 | 无 | server 900m / pg 700m / redis 256m |
| SSH 爆破防护 | 无 | fail2ban，`ignoreip` 仅 loopback |
| swap 换页 | — | `si=0 so=0`，无压力 |

所有改动均先备份到 `/root/aa-hardening-backup-<时间戳>/`。

## 迁移记录（2026-09-16）

从旧主机迁到新主机：

- 传输 `pg_dump`（99,358,786 字节）与 Docker 镜像，MD5 / SHA256 均逐字节校验一致。
- 恢复后数据：1 个账号、2 个连接器、945 个会话、22 万余条时间线条目、192 个项目。
- 附件存储卷为空（无上传文件），因此只需迁移数据库。
- 数据库密码与签名密钥沿用旧值，**已登录设备与连接器无需重新登录**。
