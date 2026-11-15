# closex.cc 新中转服务器与定向发布实施计划

**目标：** 使用域名连接新的生产中转服务器，更新 macOS 客户端、Linux Connector 及 Android APK，恢复可实际下载的下载页。

**架构：** 保留现有 API、数据库、账号、Connector ID 和签名密钥。沿用已上线的 Web/桌面降频修复；本次不增加队列推送事件。按用户最终要求发布 macOS Universal、Linux Connector 和 Android Release APK，不构建或发布 Windows、Linux 桌面、iOS 安装包。

**技术栈：** Python Connector、Electron/Next.js Desktop、Docker Compose、SSH、rsync。

## 1. 核实与清理

- 实测域名和账号登录、三台目标设备配置和在线状态。
- 对照 `/private/tmp` 的带宽排查会话与生产容器源码，区分源码版本、线上版本及安装包版本。
- 只清理经校验与完整迁移副本重复的传输分片，以及没有容器使用的构建镜像；保留数据库、业务卷、备份和同机 VPN 服务。

## 2. 代码和回归检查

- 为 Web / Desktop 的 8 秒队列轮询、后台暂停、恢复可见后刷新、30 秒权限目录轮询补充回归检查。
- 下载页生成器支持指定平台，仅展示本次确实上传的三个安装包。
- 增加 macOS + Connector + Android 定向发布入口，断点续传、SHA256 校验成功后才原子更新下载页。
- Desktop / Renderer / Connector / Android 独立升 PATCH 至 2.1.2；其他平台不发版。不修改跨端契约。

## 3. 构建与发布

- 执行 Desktop main 测试、Renderer typecheck、Connector 测试及发布工具测试。
- 构建 Universal DMG、源码 Connector 压缩包和 Android Release APK，校验包内版本和源码。
- 上传新主机，核对服务端 SHA256 和域名下载响应、Range 下载内容。
- 未具备 Developer ID / 公证条件时，明确标注签名边界；不得将未构建的其他平台宣传为已发布。

## 4. 设备更新与登录

- 查看运行中任务，避免中断用户任务。
- 更新 underground 的 macOS 客户端，以及 2080ti-server / jupiter-B365M-POWER 的现有 systemd Connector 安装。
- 保留各自配对和运行时配置；通过域名重新建立连接。仅在账号会话失效时重新登录，不随意旋转 Connector token。
- 更新部署主机、设备服务路径、清理结果、下载地址和实际验证边界；所有密码、令牌和私钥均不写入仓库。

## 实际结果（2026-11-15）

- 已发布三个 2.1.2 产物并验证完整服务端哈希与公网 Range 下载；未发布其他平台。
- underground 与 2080ti 已运行 2.1.2；B365 因内置 DSH 的 MeshCall 桥接兼容问题恢复原 2.1.0，并刷新插件域名登录。三机在线且已配置 Runtime 均 running。
- B365 DSH 恢复过程中修复了缺失的 `/dev/urandom`，保留 sandbox；未发送真实 Agent 测试任务。
- 清理后上传完成的根分区可用空间约 3.9 GB；临时上传实现与 token 均已撤销。
- 完整测试统计、签名限制及尚未覆盖的图形 / 真机验证见 [发布说明](../releases/2.1.2.md)。
