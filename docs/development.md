# v2 开发指南

## 分支与环境

`main` 是 v2 主线，新功能从最新 `main` 创建分支。`v2` 保留用于发布过渡，不再把旧 `main` 的 API 和数据库假定套用到当前主线。历史资料见[升级指南](upgrading.md)。

使用 Python 3.12+、uv、Node.js 22、Corepack/Yarn。Docker 源码启动器需要 Docker Compose；Android 需要 JDK 17 与 Android SDK，iOS 需要 Xcode。各子项目独立安装依赖，不在仓库根目录运行一个假设存在的总构建命令。

## Web 与 Server 本地联调

从根目录手动启动：

```bash
./local-up.sh
```

它启动 Docker PostgreSQL/Redis、迁移数据库，再在前台运行 Server 和 Web。默认 Web 为 `http://127.0.0.1:5174`，Server 为 `http://127.0.0.1:8000`，PostgreSQL/Redis 端口为 `55432` / `56379`。日志在 `.local-dev/logs/`。按 Ctrl-C 停止本次启动的服务。

- `--skip-install`：复用依赖。
- `--with-connector`：一起启动 Connector；默认不启动。
- `--reload`：启用 Server 热重载；默认关闭以减少实时连接中断。
- `--listen`：监听局域网地址；默认只监听回环。
- `--reset-data`：删除本地数据库数据卷，仅在明确不要这些数据时使用。

## Desktop

仅开发 Desktop，默认连接 Cloud：

```bash
cd desktop-workbench
yarn install
yarn dev
```

联调本地 Server 与 Desktop，在根目录手动运行：

```bash
./desktop-local-up.sh
./desktop-local-up.sh down
```

此启动器使用 Server `8000` 和 Desktop `5184`，会释放这两个应用端口的既有监听者；停止后数据库容器仍保留运行。更多环境变量与生命周期见 [Desktop README](../desktop-workbench/README.md)。

## Headless 检查

以下检查不要求打开 Electron、浏览器或移动模拟器。先在对应子项目安装依赖；只运行与修改有关的检查，不必为文档改动重新构建所有安装包。

```bash
cd server
uv sync
uv run ruff check . --exclude .venv
uv run pytest -q
```

```bash
cd connector
uv sync
uv run ruff check connector tests
uv run pytest -q
```

```bash
cd web-next
yarn install
yarn test
yarn typecheck
yarn protocol:check
```

```bash
cd desktop-workbench
yarn install
yarn test:main
yarn renderer:typecheck
yarn workspace agents-anywhere-desktop-renderer test
yarn workspace agents-anywhere-desktop-renderer protocol:check
```

Server 测试应使用隔离测试环境，不要把生产数据库 URL 带入测试进程。Headless 检查通过不代表真实安装、OAuth 回调或远程 Runtime 会话已验证；这些按变更影响单独记录。

## 构建安装包

在 `desktop-workbench/` 运行：

```bash
yarn dist:mac   # 在 macOS 构建 Universal DMG
yarn dist:win   # 在 Windows 构建 x64 NSIS 安装包
```

签名、公证、架构选择及缓存重试见 [Desktop Packaging](../desktop-workbench/README.md#packaging)。凭据从环境或安全存储注入，不能提交到源码或文档。Android 的命令和签名边界见 [Android README](../android/README.md)。发布步骤见 [2.0.0 发布说明](releases/2.0.0.md)。

## 客户端版本号

每次修改客户端都必须提升版本号，两端要一起改，不能只改其中一端：

| 位置 | 字段 | 说明 |
| --- | --- | --- |
| `desktop-workbench/package.json` | `version` | 决定 DMG 文件名与 `app.getVersion()`。 |
| `android/app/build.gradle.kts` | `versionName` | 用户可见版本，与 Desktop 保持一致。 |
| `android/app/build.gradle.kts` | `versionCode` | 必须每次递增，否则 Android 不把新包当作升级。 |

补丁位每次加一，按十进制递增：`2.0.1` → `2.0.2` → … → `2.0.9` → `2.0.10` → `2.0.11`。不要用 `2.0.10` 表示 `2.0.1` 之后的下一个"十位"，也不要把 `versionCode` 直接等同于版本号字符串。

改完版本号后重新构建安装包，并按 [升级指南](upgrading.md) 与发布记录说明分发方式。只改源码不重新打包不会影响已发布的二进制。

## 会话时间线约定

两端渲染同一套服务端时间线条目，规则要保持一致，避免同一个会话在两个客户端上读起来不同。

### 时间戳

每个条目显示它被服务端首次看到的时间，格式 `[YYYY-MM-DD HH:mm:ss]`，读者本地时区：

- Desktop：`desktop-workbench/renderer/src/components/session/timeline-timestamp.ts`。
- Android：`android/app/src/main/java/com/agentsanywhere/app/feature/sessiondetail/TimelineTimestamp.kt`。

要点：

- 格式固定为数字，不使用 `toLocaleString` 一类的本地化样式：时间戳是用于比较的数据，不应随语言改变字段顺序。
- 无法解析的值渲染为空，**不要渲染 `Invalid Date`**。
- 乐观消息（服务端尚未确认）没有时间戳，渲染为空而不是编造一个时间。
- 每个条目都必须显示时间戳，包括折叠状态下的过程块，否则读者无法判断某次动作的起始时刻。

### 过程折叠

一个回合 = 一次用户请求 + 该回合的全部过程（推理、工具调用、文件变更、子 Agent 调用、重连记录） + 最终回复。

- 过程折叠成一行，**默认收起**，行上保留摘要（如"推理了 1 次，执行了 2 次工具"）。
- 正在执行的回合展开，**回合结束（收到最终回复）后自动收起**。
- 读者手动展开的块不被自动收起覆盖：显式选择优先于默认行为。
- 两端的判断依据相同——运行时状态是否处于活动态；新增状态时必须同步更新两处。


