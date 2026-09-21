# DSH 增量同步：机制与完整性保证

结论：**增量同步已经在工作**，并且有严格的完整性校验。下面是核实过程。

## 已确认在增量同步

### 1. DSH 2.0.13 支持检查点

订阅时返回 `checkpointVersion: 1`。连接器据此启用 `durable_checkpoints`
（`subscription.get("checkpointVersion") == 1`）。

> 注：升级到 2.0.13 之前该字段返回 `None`，即增量不可用，只能全量。

### 2. 检查点已持久化

`~/.agents-anywhere/conn_mcFSUxwkhrkT8g/rti_O9YH4FA7EMhem7R_/sync-state.json`
存有 **91 个 DSH 会话检查点**，每个包含：

| 字段 | 含义 |
| --- | --- |
| `throughSeq` | 已读取到的历史位置 |
| `historyHash` | 该段历史的 SHA-256 指纹 |
| `settled` | 该范围是否完整 |
| `version` | 检查点格式版本 |

### 3. 实测：重新订阅不会重传历史

新开一个订阅，45 秒内只收到 **1 条通知、0 条快照条目** —— 没有重传历史。

## 完整性保证（关键）

你的担心是「同步完之后数据不完整」。这一点由三方校验共同保证：

### 桥接端：序列缺口会直接报错

```js
if (throughSeq >= 0 && Number(event.seq) !== throughSeq + 1)
    throw new Error("DSH event sequence gap");
```

每个事件都必须严格递增 1。**少一个事件就抛错**，不会静默跳过。

### 桥接端：恢复必须精确匹配

```js
function matchesCheckpoint(value, projection) {
    return checkpoint.version === 1
        && checkpoint.projectionVersion === 2
        && checkpoint.settled === true && projection.settled
        && checkpoint.throughSeq === projection.throughSeq
        && checkpoint.historyHash === projection.historyHash;
}
```

`throughSeq`、`historyHash`、`settled` 必须**全部精确相等**才认定为可恢复。

### 桥接端：无论如何都会读完剩余历史

```js
resumed = matchesCheckpoint(checkpoint, projection);
if (resumed) projection.drain();
await replayHistory(projection, log, this.abort.signal);   // 在 if 之外，始终执行
```

最后一行**在 `if (resumed)` 之外**，所以即使检查点不匹配，也会完整重放剩余历史。

### 连接器端：非法检查点一律拒绝

实测 7 种损坏形式全部被拒（版本错误、投影版本错误、seq 非整数、
hash 长度不对、hash 非十六进制、settled 非布尔、根本不是对象）。

**失败方向永远是「重新读取」，绝不是「跳过数据」。**

## 实测数据

| 项目 | 值 |
| --- | --- |
| DSH 检查点 | 91 个（89 settled，2 unsettled 会重新读取） |
| 重新订阅时重传 | 0 条历史 |
| 上行带宽 | 19.4 KB/s（链路限制，与增量无关） |

## 为什么之前仍然很慢

因为**首次全量同步**（没有检查点时）必须先上传完整历史，最大的会话有 14.2 MB。
在 19.4 KB/s 下需要数分钟，超过桥接的 60 秒确认预算。

这正是 2.0.11 修复的三个缺陷所放大的问题：上传超时过短、运行时串行、
服务端能力重发布阻塞响应。修复后首次全量同步能完成并写入检查点；
**之后的每次同步都是增量的**。
