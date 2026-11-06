# Web 面板与接口

[架构导航](README.md) · [文档导航](../README.md)

Web 配置和部署操作统一见 [README](../../README.md)。本页记录面板实现与安全边界，不复制配置默认值表。

## 生命周期

[main.py](../../src/main.py) 按 `[Web] web_enable` 调用 [web.py](../../src/web.py) 的 `run()`；后者不是第二启动入口。

- 启动前读取配置、检查实际监听地址和认证，再进入可选后台模式。
- 守护线程运行 `recording.engine.main(non_interactive=True)`，主线程通过 uvicorn 执行 [routes.py](../../src/api/routes.py) 的 `create_app()` 所创建的 Starlette 应用。
- 默认不自动开始录制，用户通过面板的全局开关控制；`DOUYIN_WEB_AUTO_START_RECORDING` 是独立的录制自动开始选项，不是面板启用开关。
- uvicorn 处理主线程退出信号；`finally` 收尾后处理线程池、FFmpeg 子进程和 HTTP 连接池。

## 后台运行模式

`web_show_console = false` 时，启动引擎前调用 `_enter_background_mode()`，将 stdout/stderr 重定向到 `logs/web_console.log`（行缓冲），并重建 Loguru 控制台 sink。日志进入后台模式时检查大小，超限截留尾部；不是持续轮转器。

Windows 通过 `GetConsoleWindow()` / `ShowWindow()` 隐藏控制台；kernel32 / user32 句柄缓存，窗口句柄按指针宽度读取。终端编码共用 [console.py](../../src/support/console.py)，不再维护旧 `_fix_encoding()` 的平行说明。恢复显示需修改配置并重启。

## API 与请求模型

[routes.py](../../src/api/routes.py) 的 `_route` 适配 Starlette 路由、模型解析与线程池派发；[models.py](../../src/api/models.py) 使用 dataclass 请求模型并维持 422 输入错误语义。这里的请求模型不是主配置使用的 pydantic 模型。

主要路由如下；完整路由以实现为准，不维护第二份逐参数 API schema。

| 路由 | 方法 | 功能 |
| --- | --- | --- |
| `/api/login`、`/api/logout` | POST | 登录发 Token / 注销 |
| `/api/auth/status` | GET | 认证状态 |
| `/health` | GET | 公开探活，返回状态与版本 |
| `/api/status`、`/api/status/stream` | GET | 状态快照 / SSE 状态推送 |
| `/api/recording/toggle` | POST | 全局开始/停止录制，停止时触发运行日志归档 |
| `/api/rooms` | GET/POST/PUT/DELETE | 查询、新增、修改和删除房间；修改/删除不使用旧文档中的 `{url}` 路径参数 |
| `/api/rooms/toggle` | POST | 启用或禁用房间 |
| `/api/rooms/quality` | PUT | 修改房间画质 |
| `/api/rooms/qualities` | GET/PUT | 画质选项查询与编辑 |
| `/api/config` | GET/PUT | 脱敏读取 / 校验写入配置 |
| `/api/language` | GET/PUT | 查询或切换语言 |
| `/api/files`、`/api/files/download` | GET | 文件列表与下载 |
| `/api/logs` | GET | 读取日志；旧 `/api/logs/stream` 不是当前接口 |
| `/api/danmaku` | GET | 弹幕监控快照 |

配置和文件操作由 [config.py](../../src/api/config.py) 提供，录制状态通过 [status.py](../../src/recording/status.py) 获取；不反向导入入口。接口冒烟操作见 [开发指南](../development.md#web接口冒烟测试)。

## 安全边界

- 默认未启用认证时只允许回环监听；非回环地址会拒绝启动，而不只是旧 wiki 所写的「输出告警」。显式设置 `DOUYIN_WEB_ALLOW_INSECURE=1` 才能绕过此启动保护，不建议部署时使用。
- 登录失败限流；认证变更复验当前口令，密码以 PBKDF2-HMAC-SHA256 保存；密码或认证开关变化吊销已有 Token。
- Host 检查与写操作的 Origin 同源检查是两层保护：Host 允许已登记域名、IP 字面量及无点号单标签；Origin 还校验实际绑定地址、端口和登记项。通配监听时，域名/IP 能通过 Host 不等于写操作的 Origin 也被放行。
- 只有受信任直连代理才参与 `X-Forwarded-For` 真实 IP 解析；不直接相信任意请求的转发头。
- 配置读取遮罩敏感项，写入拒绝把 `***` 当真实值或静默清空敏感项；自定义脚本等危险键禁止经 Web 修改，不存在旧 wiki 所述仅未认证时才禁止的分支。
- 配置目标和值、房间字段过滤换行；主配置按 TOML 存储，URL 列表仍需防注入。文件下载用 `_is_within()` 防目录穿越，异常及推送 URL 日志脱敏。

回归约束统一见 [Web 已知坑](../agent-reference/known-pitfalls.md#web-面板与接口层) 和 [凭据脱敏](../agent-reference/known-pitfalls.md#凭据与敏感配置脱敏)。

## 前端

[index.html](../../web/index.html) 定义仪表盘、直播间、配置视图；[app.js](../../web/app.js) 管理 Token、API、状态推送和渲染；[style.css](../../web/style.css) 提供明暗主题、响应式布局与画质降级高亮；[motion.js](../../web/motion.js) 处理动效。原生静态资源不需要 npm 构建。

录制表格展示名称、设置/实际画质、开始时间、已录时长，不一致时用 `.quality-down` 标红。移动端采用 `viewport-fit=cover`、安全区 `env(safe-area-inset-*)` 和 `100dvh`（`100vh` 回退）；窄屏顶栏换行、标签页及表格在所属容器内横向滚动。相关历史改造见 [早期实现记录](../../ARCHIVE/changelog/code-wiki-history-zh.md)。

> 返回[架构导航](README.md) · [文档导航](../README.md)。
