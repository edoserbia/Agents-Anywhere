# closex.cc 间歇性 502：从本机连接器日志读出来的事实

这份文档记录的是**用户本机连接器日志**里的证据，以及它意味着什么。它不解释 502 的
**成因**——那需要登上主机才能确定，文档末尾给出了具体命令。

## 怎么得到的

连接器在 `~/Library/Application Support/Agents Anywhere/logs/` 下按会话写 JSONL。
每一条 `WARNING` / `ERROR` 都带 `time`、`level`、`name`、`message`，出错时还有 `exception`。

```bash
cd "$HOME/Library/Application Support/Agents Anywhere/logs"
python3 - <<'PY'
import json, glob, datetime
def parse(t):
    return datetime.datetime.fromisoformat(t.replace('Z', '+00:00')).astimezone(datetime.timezone.utc)
stamps = []
for path in sorted(glob.glob('connector-*.jsonl')):
    for line in open(path, encoding='utf-8'):
        if '502' not in line:
            continue
        try: r = json.loads(line)
        except Exception: continue
        if r.get('time'): stamps.append(parse(r['time']))
stamps.sort()
print(len(stamps), 'events', stamps[0], '->', stamps[-1])
PY
```

**注意时间戳有两种写法**：多数记录是 `+08:00`，少数是 `Z`。同一个文件里可能混用
（20 个文件里有 6 个混用），所以任何时间线分析都必须先归一化到 UTC，否则会凭空造出
相隔 8 小时的「新窗口」。本页数字已归一化。

## 读到了什么（2026-10-10）

- **698 条**记录提到 HTTP 502，跨 **9.2 小时**（06:41–15:51 UTC，即本机 14:41–23:51）。
- 归成 **44 段**（间隔超过 60 秒算新的一段）。最长的两段：
  - **543 秒**（13:13–13:22 UTC），41 条；
  - **420 秒**（08:04–08:11 UTC），32 条。
- 最后一段（15:50:45–15:51:29 UTC）在 **43 秒内有 294 条**——那是一次风暴。
- 三段代表性记录：

```
WARNING connector.server.runtime_sync
  existing session sync network failure runtime=codex session_id=sess_codex_…
  error=backend ingest temporarily unavailable: HTTP 502

ERROR   connector.server.client
  connector loop failed; reconnecting in 3.0s
  exception: HTTPStatusError("Server error '502 Bad Gateway' for url
             'https://closex.cc/api/v2/connector/auth'")

WARNING connector.runtimes.dsh.bridge.sync
  DSH event sync interrupted; resubscribing for history calibration (ConnectorNetworkError)
```

**502 来自反向代理**：说明 Caddy 是活的，是它背后的应用容器不可达。

## 现场抓到的一次（2026-10-11 00:02 本机时间）

轮询 `/api/v2/health` 抓到的真实窗口：

```
=== 16:02:29 UTC  status=502        HTTP/2 502
=== 16:02:31 UTC  status=502        server: Caddy
...（连续 19 次，每 2 秒一次）        content-length: 0
=== 16:03:10 UTC  status=502        （空 body）
```

- **连续 19 次 502，跨 41 秒**——是一次真实的连续中断，不是偶发抖动。
- `server: Caddy`：**代理是活的**，是它背后的上游不可达。
- `content-length: 0`：Caddy 的默认 502，没有上游细节。
- 同一时刻 **443 与 4001 端口都通**（下载页 200），说明**主机是活的**，
  只有应用容器不可用。

这与「容器被杀掉后正在重启」的签名一致。

## 主机内存是超卖的（算术，不是猜测）

| | |
| --- | --- |
| `postgres-next` | 700 MB |
| `redis-next` | 256 MB |
| `migrate-next` | 512 MB |
| `server-next` | **900 MB** |
| **合计上限** | **2368 MB** |
| 主机内存 | **1900 MB**（`docs/deployment-hosts.md`） |
| 留给 OS + Caddy + 共存 VPN + 下载页 | **−468 MB** |

而且每个容器的 `memswap_limit` **等于** `mem_limit`，也就是**容器没有 swap**：
到上限就是**立刻被杀**，而不是变慢。compose 文件里的注释自己写着这些上限是为了
「让失控进程死在这里，而不是触发全机 OOM——那会连带把共存的 VPN 干掉」，
**说明这台机器上已经发生过全机 OOM。**

这两件事合起来正好解释观察到的形状：**不是变慢，是成段地消失**。

## 这意味着什么

连接器上传不了事件时：

- 会话活动**到不了服务端**，客户端自然看不到新内容——这正是「只显示正在工作、没有最新活动」；
- 事件在本机排队，等后端恢复后**一次性补上**——这正是「过几秒或十几秒才收到」；
- 一段 9 分钟的窗口里，**什么都传不回来**。

也就是说：**这个症状的主要成因是服务端间歇不可用，而不是客户端或连接器的代码。**
本次分支修的那些（停等协议、缓冲溢出、按字节分批、断点续传）降低的是**正常情况下的开销**，
并且让**每次恢复后要重传的东西更少**——它们不会、也不能阻止 502。

## 它没有告诉我们什么

**502 的成因未确定。** 从本机只能看到代理给出的 502，看不到容器为什么不可达。可能的成因
（未验证）：容器被 OOM 杀掉后重启、应用崩溃、主机资源耗尽、上游被临时摘除。

## 到主机上要跑的命令（确定成因）

```bash
# 1. 容器有没有在重启？重启次数就是 502 窗口数的下界。
docker ps --filter name=server-next --format '{{.Names}}\t{{.Status}}'
docker inspect server-next --format 'restarts={{.RestartCount}} oom={{.State.OOMKilled}} exit={{.State.ExitCode}}'

# 2. 有没有被 OOM 杀掉（mem_limit 是 900m）。
docker inspect server-next --format '{{.State.OOMKilled}} {{.State.FinishedAt}}'
dmesg -T 2>/dev/null | grep -iE "oom|killed process" | tail -20

# 3. 应用自己的日志里，崩溃前有没有异常。
docker logs --since 24h server-next 2>&1 | grep -iE "error|traceback|killed|memory" | tail -40

# 4. 代理侧的日志（Caddy）会写明 upstream 的失败原因。
docker logs --since 24h <caddy-container> 2>&1 | grep -E " 502 " | tail -40
```

判定：

- `RestartCount` 在涨、`OOMKilled=true` → **内存**问题；`server-next` 的 `mem_limit: 900m`
  是硬上限，被杀后 `restart: unless-stopped` 会拉起它，这期间 Caddy 就返回 502。
- 容器没重启但 502 持续 → 应用**卡住**（事件循环被阻塞）或上游被摘除，看应用日志里有没有
  长时间不返回的请求。
- 只有 Caddy 日志有 502、应用日志正常 → 问题在网络或代理配置，不在应用。

## 与本分支的关系

本分支在服务端修过两处**可能**导致这种表现的问题（都已修，但**尚未部署**）：

- `_read_body` 在 `http.disconnect` 上死循环（实测 5001 次调用不退出）——一个卡死的请求处理
  会一直占着 worker；
- 暂存上传没有上限——内存会随上传增长（**已确认是写磁盘，不是内存**，所以这条风险比原先估计的低）；
- **ingest 请求体没有上限**——而线上那份连接器按**条数**（64 条）分批、不按字节，
  所以单个请求可以非常大，服务端会把它整个缓冲进内存。**这一条已在本分支修掉**
  （`AGENT_SERVER_MAX_REQUEST_BODY_BYTES`，默认 64 MiB；仓库里的连接器每次最多 256 KiB，永远碰不到）。
  这降低的是**峰值**，不是根因。

**它们是候选成因，不是已证实的成因。** 不要在没有主机证据的情况下把它们当成结论。
