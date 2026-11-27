# 消息删除：会话作用域与线程作用域

Reader 删除一条消息时，运行时本身没有删除能力：Codex、Claude Code 和 DSH 都只会
继续上报同一条 item。所以 AA 的删除是**本地标记**，而不是删运行时历史：

- `timeline_items.hidden_at`：item 已经落库时，标记在行上。
- `timeline_item_hides`：item 还没落库（乐观气泡被删、请求失败）时，用独立墓碑记住
  它，等运行时后来真的上报时不写进去。

这两处标记都是按 `session_id` 记的。只要一条运行时线程在 AA 里只对应一个会话，
这就够用。

## 同一条线程对应多个会话

一条 Codex / Claude 线程在 AA 里可能对应**多个** `session` 行：

- Connector 扫描到线程时，用自己派生的稳定 id（`sess_codex_…`）上报；
- 从客户端新建、或以平台身份发起的会话，保留平台自己给的 id（例如 `sess_9jsg…`）。

两者携带同一个 `external_session_id`，投影的是同一批运行时 item，item id 也相同
（Connector 的 id 由运行时 thread id 派生，不含 AA session id）。于是"删除"只在被删
的那条会话里生效：换一条同线程的会话打开，同一个 item 仍然在；重新跑一轮更好的情况是
Connector 又把整段线程时间线投影进新会话。

## 现在的语义

2.1.3 起，删除会同时写入线程作用域：

- 新表 `timeline_thread_hides`，主键 `(connector_id, runtime, external_session_id,
  item_id)`，随 connector 级联删除（`v2_45_jupiter`）。
- 写路径（`upsert_many`、完整快照替换、写入缓冲的逐条路径）读取的
  `hidden_item_ids(session_id)` 现在包含线程作用域标记，所以同线程的任何会话都不会再
  写进被删的 item。
- 读路径（`list_since` / `list_latest` / `list_before_order_seq`）额外排除
  `_thread_hidden_ids_select(session_id)`，因此**已经落库的历史重复会话**里的同一条
  消息，也会在下次读取时消失，不需要等重新同步。
- 恢复显示（`unhide`）会同时清掉会话标记和线程标记。

## 已知边界

- 删除的实时事件（`removedItemIds`）仍然按发生删除的那个 `session_id` 广播。另一个
  设备如果同时打开的是同线程的**另一条**会话，需要等下一次读取（切会话或刷新）才会
  消失；标记已经写入，读到的结果是对的。
- 删除不会影响运行时历史、本地 rollout 文件或其他客户端自己的数据库；它只决定 AA
  时间线怎么显示。
- 迁移只加表，不改已有行；被删过但已落库的 item 依赖新读路径过滤，不回填数据。
