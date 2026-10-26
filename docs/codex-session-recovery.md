# Codex 会话在重连后报错：排查与恢复

## 区分两种写锁冲突

“该 Codex 会话正由其他进程持有写入权限”表示原生 Codex 的独占写锁冲突，
不表示合同/代码项目或会话历史已损坏，也不能直接判定 Server 数据库出错。

- **AA 自身残留的 owner**：旧 Connector 客户端被停止、认证失败退出或启动被取消，
  但其 native app-server 没有关闭。新客户端恢复原会话时会与旧进程冲突。
  2026-10-26 本机 受影响的既有会话 确认属于这一类：持锁进程和当前会话进程
  都是同一个 Connector 的子进程，但创建时间不同；IPC router 当时不在线。
- **外部 Codex App / IDE 的合法 owner**：使用 [IPC owner/follower](codex-ipc-takeover.md)
  通过原 owner 执行消息和审批，不强抢写锁。IPC 必须可达且方法版本兼容。
  原生终端 TUI/daemon 不等同于桌面 IPC；不可达的持锁者仍需在原客户端正常释放。

锁文件存在不等于锁仍被占用；不要直接删除 `.lock` 文件，或删除历史来绕过问题。

## 实现保证

- Connector 整体停止/重启或认证失败退出时，停止扫描与本机入口，关闭它管理的
  Runtime、native app-server 和 IPC；普通后端网络重连不会重启原生 Runtime。
- Runtime 启动被 RPC 取消时，同样清理半启动的 native 进程；断开的后端不能阻止清理。
- 发送/压缩前的初始化、缓存会话的轻量读取探针、thread/resume 遇到已关闭的
  transport，最多重建一次连接并重新准备；清空旧连接的 loaded-thread 缓存。
- 正式 turn/start 的响应如果在断线中丢失，消息可能已被接受。AA 修复连接供后续
  命令使用，但不会自动重发该消息，避免重复执行；应先确认会话历史再决定是否重试。

## 升级既有故障实例

代码升级不会热替换正在运行的 Python/Codex 进程。旧版本已经遗留的持锁者也不
具备新清理逻辑。升级后应在当前任务结束后完整退出并重新打开 AA，使旧 Connector
进程树释放。不要在仍需继续的 Agent 回合中强杀它的 app-server。

排查时可用 `lsof ~/.codex/thread-writer-locks/<thread-id>.lock` 配合 `ps` 核对
父进程，而不是猜测哪个客户端持有写锁。不要输出 connector token、环境变量密钥
或完整用户消息。原生会话与项目文件不需要重建。

## 验证边界

回归测试覆盖真实子进程文件锁释放、同进程 Stop/Start、认证失败、取消启动、
普通网络重连保留 Runtime、初始化/resume/缓存 transport 断线、并发恢复、
不可用的新连接限次重试及未知发送结果不重放。IPC 测试覆盖 router 重启与重新跟随。
这些不代替升级后在真实安装版上执行休眠/唤醒、用户消息与审批流程的验收。
