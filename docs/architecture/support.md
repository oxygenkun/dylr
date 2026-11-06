# 共用支撑

[架构导航](README.md) · [文档导航](../README.md)

## 共用工具与职责归属

`support/utils.py` 保留与业务无关的通用能力；配置、网络格式和平台错误处理位于各自职责包。

| 模块 | 主要能力 |
| --- | --- |
| `src/support/utils.py` | `atomic_write_text()`、`check_md5()`、`remove_emojis()`、`check_disk_capacity()`、`generate_random_string()`、`mask_credentials()` |
| `src/support/console.py` | `configure_encoding()` 与 `Color` |
| [配置存储](configuration.md) | TOML 读写、默认值、房间列表编辑与备份 |
| `src/network/urls.py` | `format_cookies()`、`normalize_proxy_url()`、`parse_jsonp()`、`query_values()` |
| `src/streams/errors.py` | `trace_error_decorator()`、`trace_error_decorator_or_none()` |
| `src/runtime/javascript.py` | `get_compiled_js()`、`run_js_async()`、`run_node_script_async()` |

## 日志模块 (`src/support/logger.py`)

**职责**: 基于 Loguru 配置结构化日志

**日志输出**:

- **控制台**: 彩色日志输出（`custom_format`，不含房间列）
- **`logs/streamget.log`**: DEBUG 级别（排除 INFO），行结构 `时间 | 级别 | 房间 | 模块:函数:行号 - 消息`
- **`logs/PlayURL.log`**: INFO 级别（仅直播流地址），行结构 `时间 | 房间 | 消息`

**房间关联字段 `extra[room]`（2026-09-20 新增）**:

- 房间线程的全部日志行带一个稳定关联列，用于从多房间交织落盘的日志里切出单个房间的链路（边界见 [日志约束](../agent-reference/known-pitfalls.md#日志控制台与后台模式)）
- 实现：`ROOM_FIELD` + `set_room_context()` / `get_room_context()` 读写一个 `ContextVar`，由 `logger.configure(extra={ROOM_FIELD: ""}, patcher=_room_patcher)` 注册的全局 patcher 在**调用线程**内写进 `record["extra"]`（故 `enqueue=True` 不串味）；调用点显式 `bind(room=...)` 优先于线程级兜底
- 房间线程在 `src/recording/session.py::start_record` 入口绑 `序号N`、解析出主播名后升级为完整 `record_name`；`set_room_context("")` 为解绑
- `extra` 默认值覆盖未绑定房间的日志，避免格式化时缺少 `room` 字段；回归锁 `tests/test_logger_room_context.py`

**日志文件开关**:

- 通过 `config/config.toml` 的 `是否启用日志文件(是/否)` 控制
- 默认启用，保持向后兼容
- logger.py 在初始化时直接读取配置（不依赖 src/main.py 执行顺序）

**日志轮转**: 300 KB 自动轮转，`retention=3`（以当前 logger 配置为准）。停止时归档由 `src/app/log_archive.py` 负责，重定向控制台见 [Web 后台模式](web.md#后台运行模式)。

## 消息推送模块 (`src/notifications/msg_push.py`)

**职责**: 支持多种消息推送渠道

**支持渠道**:

| 渠道 | 函数名 | 说明 |
| --- | --- | --- |
| 钉钉 | `dingtalk()` | 群机器人推送 |
| 微信 | `xizhi()` | Server酱 / WeChat |
| Telegram | `tg_bot()` | Bot 消息 |
| 邮件 | `send_email()` | SMTP 协议 |
| Bark | `bark()` | iOS 通知 |
| NTFY | `ntfy()` | 开源推送服务 |
| PushPlus | `pushplus()` | 微信推送平台 |

## 国际化模块 (`src/support/i18n.py`)

`support/i18n.py` 使用中文源码原文和 `i18n/en_US.json` 英文目录，支持 `zh_CN` / `en_US`；语言别名统一归一，英文目录缺失或损坏时回退中文。

- `tr(template, **kwargs)` 先查译文再替换占位符，供业务提示和日志调用。
- `translated_print` 翻译项目源码的 print 参数；由 `recording.engine.initialize_process()` 显式安装，不在导入 CLI 入口时安装。
- `set_language()` 热切换进程内译文，Web 的 `GET/PUT /api/language` 和引擎配置热加载共用该能力。
- 源码与冻结资源路径分别支持项目根目录和 `_internal/`；翻译资源随入口打包。

翻译文案分布在 `recording/`、`streams/`、`danmaku/`、`network/`、`notifications/`、`runtime/`、`config/` 和 Web 入口。直接编辑英文 JSON 目录，无需 gettext 编译；`tool/extract_i18n_strings.py` 提取并比对缺失文案，相关回归用例在 `tests/test_i18n*.py`。

## 外部运行时与签名资源

FFmpeg 与 Node.js 优先使用已有程序；需要安装时，由 `src/runtime/ffmpeg.py` / `src/runtime/node.py` 调用共用安装器，按 `src/external_resources.json` 直接下载并验证。Docker 使用镜像内组件。构建用法见 [构建与发布](../build-and-release.md)。

`src/runtime/javascript.py` 集中管理脚本校验、编译缓存和执行超时；JS 资源与资源清单中的 `javascript_sha256` 同步。旧 Weverse pip 包会引入不可用的 pycrypto，不能据历史文档恢复该依赖。

> 返回[架构导航](README.md) · [文档导航](../README.md)。
