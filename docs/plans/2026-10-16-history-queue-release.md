# History and queue release implementation plan

**Goal:** 修复历史导航的可见性及十条分页，验证现有原生队列修复并发布客户端与服务端。

**Architecture:** 沿用时间线历史 API，由历史导航按用户请求数自动补页，而非按工具事件页数分页。Desktop/Web 的历史侧栏避开覆盖式标题栏；Android 弹层使用同样的目标条数、失败重试与无进度保护。保留旧 Bridge 的持久化队列回退。

**Tech Stack:** React / TypeScript、Kotlin / Compose、Python / FastAPI、Electron。

## Tasks

1. 为历史导航补页决策（不足十条、跨工具页、更多、失败、无进度）添加回归测试。
2. Desktop/Web 修复固定标题/收起按钮与自动补足分页；session/reset 隔离。
3. Android 同步自动补页与失败重试；历史页没有更老事件时停止。
4. 跑相关单测、类型检查；真实 Electron/Android 界面验证开关、10/20 条与队列管理。
5. 检查 Bridge 当前发布版本、DSH Desktop 构建链路及授权；不替换用户已安装的应用目录。
6. 更新版本和发布说明，构建各安装包；仅验证通过后上传，校验哈希并更新下载页。
7. 部署服务端/Web，仅重建应用服务，保留数据库与上一个镜像以便回滚；核对健康检查与下载链接。

## Verification boundaries

本地 fake API UI 验证和真实 DSH 原生队列验证分别记录。不得将前者报告为生产原生联通验证；若 npm/DSH 发布缺少权限，明确报告阻塞部分。
