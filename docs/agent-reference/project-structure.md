# 项目结构

本文按当前源码列出目录、文件职责与主要调用关系。快速概览见 [README](../../README.md#-项目结构)，实现说明见 [CODE_WIKI](../../CODE_WIKI.md#核心模块详解)，工程约定以 [AGENTS.md](../../AGENTS.md) 为准。历史审查和更新日志中的路径描述当时的代码布局。

## 仓库目录

| 路径 | 内容 |
| --- | --- |
| `src/` | Python 运行时代码、平台协议与签名资源，详见下方源码树 |
| `web/` | Web 面板静态资源：`index.html`、`app.js`、`style.css`、`motion.js` |
| `i18n/en_US.json` | 英文翻译目录；中文直接使用源码原文 |
| `tests/` | Python 回归、集成与可手跑的真机采集脚本；文件名不要求随源码子包重命名 |
| `tests/frontend/` | Node 内置 `node:test` 用例及对应的 Python 包装；部分包装位于 `tests/` 根目录 |
| `tests/fixtures/`、`tests/golden/` | 测试辅助程序与 FFmpeg 命令基线 |
| `tool/` | 构建、覆盖率、翻译提取、冒烟与独立录制工具 |
| `typings/execjs/` | PyExecJS 类型存根 |
| `docs/development.md`、`docs/development-en.md` | 中英文开发指南 |
| `docs/faq.md`、`docs/faq-en.md` | 中英文常见问题（Q & A） |
| `docs/agent-reference/` | 目录、已知坑、实测依据和共享经验参考 |
| `docs/changelog/` | 中英文发布说明与历史架构日志、发布说明归档 |
| `docs/worklog/` | 历史审查、诊断与提案 |
| `.agents/skills/` | 入库的项目技能，清单见 AGENTS.md |
| `.github/` | Issue / PR 模板、共用重试动作、CI、发布与镜像审计工作流 |
| `pyproject.toml`、`uv.lock` | 项目元数据、唯一版本声明、依赖与开发工具配置、依赖锁定 |
| `Dockerfile`、`docker-compose.yaml` | CLI / Web 镜像构建与服务编排 |
| `.gitignore`、`.dockerignore` | 仓库与镜像构建上下文排除项 |
| `.pre-commit-config.yaml`、`.coveragerc-concurrency` | prek 提交检查与并发专项覆盖率配置 |
| `README.md`、`README_EN.md` | 中英文使用、部署指南与独立文档入口 |
| `CODE_WIKI.md` | 架构与实现说明（中文） |
| `AGENTS.md` | 工程约定、检查时机与完成定义 |
| `index.html` | 独立 M3U8 播放器，与 `web/` 面板入口分开 |
| `LICENSE` | MIT 许可证 |

## 源码目录

子包的 `__init__.py` 除下方特别列出的入口外省略。

```text
src/
├── __init__.py                  # 显式 Node 初始化、弹幕平台注册表与工厂
├── main.py                      # 唯一入口：按 [Web] web_enable 选择纯录制或面板 + 录制
├── web.py                       # 面板实现（引擎线程与 uvicorn 生命周期），非启动入口
├── app/                         # 应用生命周期
│   ├── startup_cleanup.py       # 启动时的陈旧字节码清理
│   ├── graceful_shutdown.py     # 退出协调与线程登记
│   └── log_archive.py           # 停止后的运行日志归档
├── config/                      # 配置处理，与根目录 config/ 数据分开
│   ├── booleans.py              # 布尔 token 解析与格式化
│   ├── store.py                 # TOML 存储、pydantic 类型校验、按内容摘要热加载与旧 INI 迁移
│   ├── reader.py                # 旧 INI 的容错解析与重复键诊断（仅迁移路径使用）
│   ├── files.py                 # TOML 读写、URL_config.ini 替换与行去重
│   └── io.py                    # 配置缺省值写回、房间编辑与脱敏备份
├── recording/                   # 录制业务
│   ├── engine.py                # 进程初始化、配置热加载与房间线程调度
│   ├── session.py               # 单房间循环、直下、FFmpeg 命令与看门狗
│   ├── state.py                 # 共享配置、路径、状态、锁与调度器引用
│   ├── scheduler.py             # 网络/录制并发容量与 host 熔断
│   ├── process.py               # FFmpeg 子进程登记与清理
│   ├── postprocess.py           # 分段、转封装、转码与时间字幕
│   └── status.py                # 录制状态快照与终端状态展示
├── streams/                     # 视频流解析，与弹幕协议分开
│   ├── dispatch.py              # URL 准入、平台分派、短链归一与房间上下文
│   ├── spider.py                # 平台接口请求与直播数据获取
│   ├── stream.py                # 平台流地址、画质映射与实际画质回采
│   ├── selection.py             # 同步选源探针、可达性校验、退避与降级
│   ├── room.py                  # 抖音直播间/主页信息解析
│   ├── ab_sign.py               # 抖音 a_bogus 签名
│   ├── ttwid.py                 # 抖音访客凭据
│   └── errors.py                # 同步/异步解析异常记录与契约兜底
├── danmaku/                     # 弹幕业务
│   ├── base.py                  # 弹幕客户端接口与消息数据结构
│   ├── collector.py             # 线程化采集、视频时间轴与字幕接线
│   ├── monitor.py               # 实时监控枢纽与 JSONL 输出
│   ├── srt_writer.py            # SRT 字幕分片写入
│   ├── compression.py           # 共用 gzip/zlib 有界解压
│   └── platforms/               # 弹幕协议实现，不是视频流平台分派
│       ├── bilibili.py          # B站（含平台专用 Brotli 解压）
│       ├── douyin.py            # 抖音
│       ├── douyu.py             # 斗鱼
│       ├── huya.py              # 虎牙
│       ├── twitch.py            # Twitch
│       ├── _tars.py             # 虎牙 Tars 编解码
│       └── _xbogus.py           # 抖音弹幕签名
├── monitors/                    # 账号级监控，与弹幕监控分开
│   ├── registry.py              # 监控实例及新增房间回调
│   ├── douyin_follow.py         # 抖音关注列表监控
│   └── douyin_dm.py             # 抖音私信监控
├── notifications/               # 业务通知
│   ├── msg_push.py              # 各推送渠道
│   └── notify.py                # 录制成功/失败反馈、推送与自定义脚本
├── network/                     # 网络传输与共用请求策略
│   ├── http.py                  # 异步 HTTP 请求、客户端池与 Cookie 请求
│   ├── targets.py               # 地址、IP、DNS 与重定向目标校验
│   ├── policy.py                # 平台 API 与拉流 TLS 策略
│   ├── credentials.py           # 类型化凭据缓存、跨线程/循环请求去重
│   ├── proxy.py                 # 代理解析与系统代理发现
│   ├── websocket.py             # WebSocket 连接、发送、心跳与重连
│   └── urls.py                  # URL、代理、Cookie、JSONP 与查询参数格式处理
├── runtime/                     # 外部运行时与签名脚本执行
│   ├── resources.py             # 读取统一外部资源清单
│   ├── installer.py             # 共用直接下载、校验、解包与安装
│   ├── ffmpeg.py                # FFmpeg 可用性检查、安装与 PATH 优先级
│   ├── node.py                  # Node.js 可用性检查与安装
│   └── javascript.py            # 签名脚本校验、编译缓存与执行超时
├── api/                         # Web 后端
│   ├── routes.py                # Starlette 应用、认证、REST API、SSE 与静态挂载
│   ├── models.py                # dataclass 请求模型与输入校验
│   └── config.py                # 面板配置、房间管理、文件访问与配置脱敏
├── support/                     # 不依赖录制状态的共用支撑
│   ├── console.py               # 终端编码与颜色常量
│   ├── i18n.py                  # 中英文翻译与语言热切换
│   ├── logger.py                # Loguru 日志与房间上下文
│   └── utils.py                 # 原子文件写入、文本、磁盘与凭据脱敏
├── proto/
│   ├── douyin.proto             # 抖音弹幕协议源定义
│   ├── douyin_pb2.py            # protoc 生成代码
│   └── douyin_pb2.pyi           # 对应类型存根
├── javascript/                  # 签名资源，执行逻辑在 runtime/javascript.py
│   ├── crypto-js.min.js
│   ├── x-bogus.js
│   ├── haixiu.js
│   ├── liveme.js
│   └── migu.js
└── external_resources.json      # 下载地址、版本、哈希、签名与 JS 哈希元数据
```

## 主要调用关系与边界

1. `src/main.py` 设置终端编码并确保配置已迁移，再按 `[Web] web_enable` 分流：关闭时调用 `recording.engine.initialize_process(cli=True)` 和 `recording.engine.main()`；开启时交给 `src/web.py` 的 `run()`，后者校验监听与认证配置，在守护线程运行引擎，主线程运行 uvicorn 与 `api.routes.create_app()` 创建的应用。`src/web.py` 只是面板实现，不是启动入口。
2. `recording/engine.py` 读取配置、热加载房间并启动各房间的 `recording.session.start_record()`。每房间独立线程，异步平台请求由各自的 `asyncio.run()` 驱动；没有进程级共享事件循环。
3. `recording/session.py` 调用 `streams/dispatch.py` 分派平台，平台实现经 `spider.py` 获取数据、`stream.py` 解析地址和画质，再由 `selection.py` 选源。FFmpeg 命令和运行监控在 session，进程清理在 process，转码等后处理在 postprocess。
4. 弹幕参数由平台分派写入房间上下文，session 经 `src/__init__.py` 工厂启动 `danmaku/collector.py`。字幕、实时弹幕监控和账号关注/私信监控分别归属 `danmaku/srt_writer.py`、`danmaku/monitor.py`、`monitors/`。
5. Web API 通过 `recording/state.py`、`recording/status.py`、`config/io.py` 和 `api/config.py` 读写状态与配置，业务模块不反向导入 CLI / Web 入口。配置写入共享 `state.file_update_lock`。
6. `support/` 提供通用支撑；`app/` 负责启动与停止收尾，不导入录制状态。配置操作归 `config/`，JS 执行归 `runtime/`，URL/Cookie 格式处理归 `network/urls.py`，平台解析兜底归 `streams/errors.py`。
7. 异步 HTTP 与同步选源探针共用 `network/targets.py` 的地址判定；网络层不导入 `api/`。通用 gzip/zlib 弹幕解压归 `danmaku/compression.py`，B站专用 Brotli 解压留在平台实现。

## 工具与运行期目录

| 工具 | 用途 |
| --- | --- |
| `tool/build_exe.py` | 统一入口（含可选面板）打包、发行归档与打包产物冒烟 |
| `tool/check_coverage.py` | 提交前逐模块覆盖率检查；阈值以脚本的 `MODULE_THRESHOLDS` 为准 |
| `tool/extract_i18n_strings.py` | 从源码提取文案并比对英文目录 |
| `tool/smoke_test.py`、`tool/smoke_web.json` | 对运行中的 Web / HTTP 接口做冒烟检查 |
| `tool/_ci_web_smoke.sh` | CI 中启动 Web、等待就绪与收尾 |
| `tool/douyin_live_recorder_standalone.py` | 纯标准库单文件录制工具，保留独立实现 |
| `tool/spike_arm64_static_ffmpeg.sh` | Linux arm64 静态 FFmpeg 调研辅助脚本 |

源码从项目根目录运行，不安装项目自身：

```bash
uv sync --locked
uv run --locked python -m src.main
# 面板由 config/config.toml 的 [Web] web_enable 控制（false = 纯录制）
```

根目录的 `config/` 保存 `config.toml` 和 `URL_config.ini`；`downloads/`、`logs/`、`backup_config/` 分别保存录制文件、运行日志和配置备份。`ffmpeg/`、`node/` 是本地运行时目录；这些路径按需创建，不要求首次检出时存在。冻结运行时以可执行文件所在目录定位用户数据，两种运行形态共享 `_internal/` 中的打包资源。
