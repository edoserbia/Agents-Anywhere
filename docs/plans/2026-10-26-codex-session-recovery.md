# Codex 既有会话接管与断线恢复

## 现场与范围

2026-10-26，本机已安装的 Connector 没有 Codex IPC 接入；对
受影响的既有会话 的既有 thread 执行 `thread/resume` 时，原 Codex
进程仍持有 writer lock，随后 AA 将发送失败投影成 session error。
进一步核对父进程链：该持锁进程也是同一个 AA Connector 的旧子进程，
并非当前正在运行的原生桌面客户端。本机当时没有可连接的 Codex IPC router。
源码确认 BackendRpcClient 的最终退出分支没有停止 RuntimeSupervisor；
启动被 RPC 断线取消时也未捕获 CancelledError 清理已创建的 native runtime。
因此不能仅凭“active writer”认定是外部 Codex App 冲突。
另有休眠后 native app-server transport 失效的恢复缺口。

不删除原生历史、不强制解除其他进程的锁、不修改项目文件，也不重置数据库。
不自动重放可能已送达的 `turn/start`。

## 实施顺序

1. 整合已有 Codex IPC owner/follower 实现及相关修复，保留本分支的
   OpenScience、队列与本机直连实现。通过原 owner 转发消息，而非抢锁。
2. 在 Connector 生命周期结束时关闭所有 runtime（不是每次 WebSocket 重连）；
   启动被取消时清理半启动的进程。退出清理不依赖已断开的后端状态发布。
3. 对 native 写操作的初始化、读取连接探针和 thread/resume 做一次有界恢复；
   正式 turn/start 不因 transport error 自动重发。
4. 回归覆盖：原客户端持有会话、router 重启、残留锁文件、初始化断线、
   resume 断线、已加载会话断线、发送确认丢失与并发恢复。
5. 运行 Connector 定向/全量测试与 lint，以及涉及资源的客户端检查。
6. 使用只读本机 IPC 探针确认受影响会话可找到原 owner；更新本机受管理
   Connector 后检查实际连接与会话状态。真实休眠和用户命令执行需分别报告。

## 选择

优先使用仓库已有的 IPC 接管代码，而不是要求用户长期退出 Codex App。
纯重启 Connector 不能消除其他进程合法持有的 writer lock；删除锁文件会破坏
互斥语义，不能作为修复。断线后只重试确定不产生用户 turn 的准备步骤。


## 执行结果

- 已整合 IPC 接管和相关后续修复，并补齐 Connector 最终退出/取消启动的资源清理。
- 已实现安全准备阶段的 transport 恢复；真实 stdio 子进程退出、真实文件锁释放的回归通过。
- 只读探针发现本机当时无可达 IPC router；因此进一步使用持锁文件的进程归属核对，
  确认旧 AA 子进程持锁，没有把不可达探针写成“成功接管”。
- 已构建并安装 Desktop / Connector 2.1.1，旧 App 留有本机备份。未重启承载当前
  排查会话的旧 Connector；既有会话的实际发送/审批和休眠验收待完整重开新版后执行。
- 测试统计和非绿灯项见 [2.1.1 验证记录](../releases/2.1.1.md)。
