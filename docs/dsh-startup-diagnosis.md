# DSH 启动慢与 "status='starting'" —— 诊断结论

## 症状
1. 每次打开 Desktop，DSH 同步很久
2. 提交任务报：`runtime instance 'rti_O9YH4FA7EMhem7R_' is not running (status='starting')`

## 根因链（实测数据）

### 1. `/sessions/list` 返回全部 949 个会话（1 MB），耗时 ~6 秒
```
limit=1   -> 949 sessions (1,041,572 bytes, 6.07s)
limit=5   -> 949 sessions
limit=50  -> 949 sessions
```
`limit` 参数被完全忽略：该路由走的是 `list_session_inventory`（无 limit 的全量清单）。
时间构成：`connect=0.11s  ttfb=1.56s  total=6.07s` —— 4.5 秒花在传输 1MB 上。

### 2. 每次 ingest 都要 POST，单次往返 0.2–6.4 秒
```
connector.heartbeat            elapsed_ms=144
session.state.updated          elapsed_ms=618-768
/sessions/list (完整)          ~6400ms
```

### 3. 桥接端 60 秒 ACK 超时
DSH bridge 内置 `ackTimeoutMs = 6e4`（60 秒），并有 `queuedEvents` 最多 700 条。

### 4. 结果：批次永远无法在 60 秒内确认
桥接日志：
```
event=sync.failed batchSeq=28 queuedEvents=90
event=sync.failed batchSeq=29 queuedEvents=700
```
50 次尝试**全部从 batchSeq 1 重新开始** —— 进度每次被丢弃。

### 5. 直接后果
`inventory.complete` 永不送达 → 连接器无法发布 `running` → 运行时永远停在 `starting`
→ supervisor 拒绝一切操作 → 你看到的报错。

### 6. 同时：Codex 全量扫描持续占用
543 个会话逐个同步，一轮 38–45 秒，间隔 30 秒 —— 循环从不空闲。

## 已修复
连接器的就绪判断加了**有时间上限的等待**：90 秒后若 inventory 仍未完成，
但流已订阅、连接仍在，就发布 `running`（摄入继续在后台进行）。
实测：DSH 状态已从 `starting` 变为 `running`，`runtime_unavailable` 错误归零。

## 仍待改进（性能）
- `/sessions/list` 应支持分页，避免客户端每次下载 1MB
