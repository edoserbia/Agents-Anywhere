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

## 成因已确定：容器自己的 900 MiB 上限（内核日志）

登上主机后（只读命令），结论明确：

```
$ dmesg -T | grep "Memory cgroup out of memory"
[Sat Oct 10 15:39:12 2026] Memory cgroup out of memory: Killed process 679567 (python)
    total-vm:1470868kB, anon-rss:733848kB
[Sat Oct 10 15:50:13 2026] ... anon-rss:731136kB
[Sat Oct 10 16:01:56 2026] ... anon-rss:730740kB
[Sat Oct 10 16:08:20 2026] ... anon-rss:730740kB 附近的同一量级
[Sat Oct 10 16:13:24 2026] ... anon-rss:727xxxkB
[Sat Oct 10 16:19:25 2026] ... （第 6 次）
```

- `constraint=CONSTRAINT_MEMCG` → **杀它的是容器自己的 cgroup 上限**，不是全机内存不足。
- `docker inspect`：`mem_limit=943718400`（**900 MiB**）、`memswap_limit` 相同（**无 swap**）。
- 每次都在 **anon-rss ≈ 727–734 MB** 被杀——就是撞上限。
- `restarts=5`；postgres / redis 都是 **Up 4 days**，只有应用容器在反复重启。

**我先前写的「主机内存超卖」是错的**，那是照着 `docs/deployment-hosts.md` 里「1.9 GB」算的，
而主机实际是 **3911 MB + 2 GB swap**，可用 2868 MB。主机很宽裕，**是容器的上限太小**。
那段算术已删除。

## 这是一个自我强化的循环

30 秒一次采样（`docker stats`），一个完整周期：

```
16:15:04  353.7MiB      16:18:41  353.9MiB
16:15:35  462.0MiB      16:19:13  765.4MiB   ← 两分钟内冲上去
16:16:06  462.0MiB      16:19:44  615.7MiB
16:16:37  462.3MiB      16:20:15   77.8MiB   ← 又被打死重启
16:17:08  354.0MiB
```

内存先在 **354↔463 MiB** 之间锯齿（GC），然后在 **约两分钟内冲到 900 以上**被杀。

击杀间隔：**11.0 → 11.7 → 6.4 → 5.1 → 6.0 分钟**，而同期有 **125 次 ingest 请求 / 5 分钟**。

**循环是这样闭合的**：容器被杀 → 连接器断线重连 → **把积压一次性补传** → 内存尖峰 → 再被杀。
所以它不会自愈，只会一直转下去。这也解释了为什么「过一会儿又能用，然后又不通」。

## 这意味着什么## 这意味着什么

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

## 为什么内存会涨到 900 MB：实测的「棘轮」

本地用真实 uvicorn + 真实路由实测「解析一个 ingest 请求体要多少常驻内存」：

| 请求体 | 常驻内存变化 |
| --- | --- |
| 1 MiB | **+8 MiB** |
| 4 MiB | **+30 MiB** |
| 16 MiB | **+148 MiB** |

而且**不会还回去**——下一次请求从上一个水平继续往上（16 MiB 那次测完，RSS 停在 371 MB）。
大约是**请求体大小的 9 倍**。

线上那份连接器按**条数**（64 条）分批、不按字节，所以单个请求可以任意大；
每来一个大的，内存地板就抬高一次，直到撞上 900 MB 被杀。**这就是棘轮。**

复现：`server/.venv/bin/python devtools/measure_ingest_memory_cost.py`

**加上限之后（同一个脚本跑出来的对照）**：

```
body  1.0 MB -> HTTP 200   rss +10 MB
body  4.0 MB -> HTTP 200   rss +31 MB
body 16.0 MB -> HTTP 413   rss +24 MB     ← 超限，不再解析
body  9.0 MB -> HTTP 413   rss  +0 MB     ← 完全不再抬高
```

同一份 16 MB 请求体：**修之前 +148 MB 且不归还，修之后 +24 MB**（只缓冲到上限就停），
稳态 **+0**。**棘轮停了。**

因此本分支给 ingest 请求体加了上限，并且**默认值是按这个实测定的：8 MiB**
（约 70 MB 成本；是仓库里连接器单次上限 256 KiB 的 32 倍，更大的走暂存分片）。
我上一轮先写的 64 MiB 是错的——单个 64 MiB 请求体约 600 MB，**足以杀死它本想保护的容器**。

## 建议的改动（**未执行**，等你一句话）

`docker/docker-compose.postgres.yml` 里 `server-next` 的 `mem_limit: 900m` 是硬约束，
而主机有 3911 MB（postgres 700 + redis 256 + caddy ~40 已占约 1000 MB）：

```yaml
  server-next:
    mem_limit: 1600m        # 原 900m
    memswap_limit: 1600m    # 保持与 mem_limit 相同（沿用「不 swap」的既有取舍）
```

这是**上限**，不是预留，所以提高它不会挤占别的服务；主机可用 2868 MB，1600 是安全的。
改完只需 `docker compose up -d server-next`。

**但它只是止血**：真正的尖峰来自「积压一次性补传」，那要靠本分支的改动
（连接器按字节分批 + 流式窗口合并；服务端限制单请求体大小）去削。两者要一起上。

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
