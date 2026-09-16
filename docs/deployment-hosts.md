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

## 迁移记录（2026-09-16）

从旧主机迁到新主机：

- 传输 `pg_dump`（99,358,786 字节）与 Docker 镜像，MD5 / SHA256 均逐字节校验一致。
- 恢复后数据：1 个账号、2 个连接器、945 个会话、22 万余条时间线条目、192 个项目。
- 附件存储卷为空（无上传文件），因此只需迁移数据库。
- 数据库密码与签名密钥沿用旧值，**已登录设备与连接器无需重新登录**。
