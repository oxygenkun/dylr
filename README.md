![video_spider](https://socialify.git.ci/y123ao6/DouyinLiveRecorder/image?font=Inter&forks=1&language=1&owner=1&pattern=Circuit%20Board&stargazers=1&theme=Light)

简体中文&nbsp;&nbsp;|&nbsp;&nbsp;[**English**](README_EN.md)

[开发指南](docs/development.md) · [常见问题（Q & A）](docs/faq.md) · [更新记录](docs/changelog/release-notes-zh.md)

## 💡 简介

[![Python Version](https://img.shields.io/badge/python-v3.14%2B-blue?logo=python)](https://www.python.org/downloads/)
[![Node.js Version](https://img.shields.io/badge/node.js-LTS_v24%2B-blue?logo=node.js)](https://nodejs.org/en/download)
[![FFmpeg Version](https://img.shields.io/badge/ffmpeg-v9.0%2B-blue?logo=ffmpeg)](https://www.ffmpeg.org/download.html)
[![Supported Platforms](https://img.shields.io/badge/platforms-Windows%7CLinux%7CmacOS-blue)](https://github.com/y123ao6/DouyinLiveRecorder)
[![GitHub issues](https://img.shields.io/github/issues/y123ao6/DouyinLiveRecorder)](https://github.com/y123ao6/DouyinLiveRecorder/issues)
[![Latest Release](https://img.shields.io/github/v/release/y123ao6/DouyinLiveRecorder)](https://github.com/y123ao6/DouyinLiveRecorder/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/y123ao6/DouyinLiveRecorder/total)](https://github.com/y123ao6/DouyinLiveRecorder/releases/latest)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Stars](https://img.shields.io/github/stars/y123ao6/DouyinLiveRecorder)](https://github.com/y123ao6/DouyinLiveRecorder/stargazers)

一款**简易**的可循环值守的直播录制工具，基于 FFmpeg 实现多平台直播源录制，支持自定义配置录制以及直播状态推送。

上游项目：[ihmily/DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder)

## ✨ 功能特性

| 功能 | 说明 |
| --- | --- |
| 🎯 **多平台支持** | 支持抖音、TikTok、YouTube、快手、虎牙、斗鱼、B站等 **51 个平台**（持续添加中） |
| 🔄 **循环值守** | 自动检测直播状态，开播自动录制，断播自动停止 |
| 🎬 **多种格式** | 支持 TS、MKV、FLV、MP4、MP3、M4A 等格式输出 |
| 🖥️ **两种运行方式** | 命令行模式、Web 管理面板模式 |
| 📊 **画质监控** | 实时检测各直播间实际画质，画质降级时自动告警 |
| 💬 **弹幕录制** | 抖音 / 斗鱼 / 虎牙 / B站 / TwitchTV 弹幕采集，按分片输出 SRT 字幕，与视频同起同停 |
| 👀 **弹幕监控** | 独立的弹幕实时查看模式（不落盘），Web 面板可查看 |
| 🏷️ **主播名自动更新** | 主播改名后自动同步 `URL_config.ini` 并重命名录制目录与文件 |
| 📱 **消息推送** | 支持钉钉、微信、邮箱、TG、Bark、NTFY、PushPlus 等推送 |
| 🐳 **Docker 支持** | 支持 Docker 容器化部署，开箱即用 |
| 🌐 **国际化** | 中文原文 / English 两种语言，Web 面板**免重启热切换** |
| ⚙️ **灵活配置** | 支持按直播间自定义画质、格式、分段录制等，配置改动热加载 |
| 🔐 **Web 安全** | Token 认证、登录爆破限流、路径穿越防护、敏感配置脱敏、未认证写保护 |

> 原生 Python GUI 和 Qt 托盘已移除，当前提供 Web 面板和 CLI；Tauri 包装留待后续实现。已有配置中的 `[GUI]` 和 `web_minimize_to_tray` 不再使用，可手动删除。

## 🚀 快速开始

### 方式一：下载运行包（推荐新手）

1. 进入 [Releases](https://github.com/y123ao6/DouyinLiveRecorder/releases) 下载最新发布的 zip 压缩包。
   每个平台各出 **lite**（不含内置 ffmpeg/node，首启时程序尝试自动获取）与 **full**（内置运行时）两包。
   附件名的**平台段**由 `build_exe.make_zip()` 取 `sys.platform` 经 `{"win32": "windows", "darwin": "macos"}`
   归一（其余一律归 `linux`），**架构段**才是 `platform.machine().lower()` 的实际取值：

   | 运行平台 | 附件名 |
   | --- | --- |
   | Windows x64 | `DouyinLiveRecorder-v<版本>-windows-amd64-lite.zip` / `-full.zip` |
   | Linux x86_64 | `DouyinLiveRecorder-v<版本>-linux-x86_64-lite.zip` / `-full.zip` |
   | **Linux arm64** | `DouyinLiveRecorder-v<版本>-linux-aarch64-lite.zip` / `-full.zip` |
   | macOS（Apple Silicon） | `DouyinLiveRecorder-v<版本>-macos-arm64-lite.zip` / `-full.zip` |

   > Linux arm64 的架构段写作 **`aarch64`**——那是 `platform.machine().lower()` 在该架构上的实际取值，
   > **不是** `arm64`；该包由四平台发布矩阵（`windows` / `linux` / `linux-arm64` / `macos`）产出。
   > lite 包缺少 FFmpeg / Node.js 时按统一资源配置直接下载，失败后可手动安装；不调用系统包管理器。
2. 解压后，在 `config` 文件夹内的 `URL_config.ini` 中添加直播间地址
3. 运行 `DouyinLiveRecorder.exe` 开始录制

### 方式二：源码运行（推荐开发者）

需要 Python 3.14+ 和 uv。

```bash
# 克隆项目
git clone https://github.com/y123ao6/DouyinLiveRecorder.git
cd DouyinLiveRecorder

# 使用锁文件安装依赖，项目直接从源码运行
uv sync --locked

# 运行程序（uv 自动使用项目虚拟环境；唯一入口）
# 是否同时启动 Web 管理面板由 config.toml 的 [Web] web_enable 决定
uv run --locked python -m src.main

```

### 方式三：Docker 运行

```bash
# 纯录制（默认；config/config.toml 的 [Web] web_enable = false）
docker compose up -d

# 面板 + 录制：把 web_enable 设为 true（并建议开 web_auth_enable）后，同一命令启动，
# 浏览器访问 http://localhost:8000（容器内还需 web_host = 0.0.0.0）

# 或本地构建并启动（APP_VERSION 取自 pyproject.toml；缺省则镜像 LABEL version 固化为空串）
docker build --build-arg APP_VERSION="$(python -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")" -t douyin-live-recorder .
docker run -d -v ./config:/app/config -v ./downloads:/app/downloads -v ./logs:/app/logs -v ./backup_config:/app/backup_config douyin-live-recorder
```

> 容器内 FFmpeg 与 Node.js 由镜像自带（apt 安装），无需挂载本地 `ffmpeg/`、`node/` 目录；
> `config/`、`downloads/`、`logs/`、`backup_config/` 通过卷挂载持久化。
> 不想本地构建时，可直接拉取 GHCR 上的预构建多架构镜像（`linux/amd64` + `linux/arm64`），
> 命令见下方「🐋 Docker 部署」的「拉取预构建的多架构镜像（GHCR）」小节。

### 手动安装 ffmpeg（Windows 用户指南）

Windows 启动时缺少 FFmpeg，会按 `src/external_resources.json` 中的地址和校验值直接下载。下载或校验失败时，可按以下步骤手动安装。

1. **下载官方 zip**：<https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip>
   需要自行核对来源时，比对该 zip 的官方摘要文档
   <https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip.sha256>（程序自动安装时校验用的就是这份）。
2. **把解压出来的 `bin` 文件夹放进程序目录并改名为 `ffmpeg`**：

| 你的运行方式 | 放置结果（务必是这一层） |
| --- | --- |
| 下载的运行包（exe） | `DouyinLiveRecorder\ffmpeg\ffmpeg.exe` |
| 源码运行 / 单文件版脚本 | `<项目根>\ffmpeg\ffmpeg.exe` |

   - **不要**保留成 `ffmpeg\bin\ffmpeg.exe`——程序只把 `ffmpeg\` 这一层目录加入查找路径，不会进子目录寻找。
3. **验证**：在 `ffmpeg\` 目录里执行 `ffmpeg -version`，能打印版本号即成功。随后重启程序，
   日志（`logs\streamget.log`）中不再出现「未安装 ffmpeg。」即为生效。

也可以改用系统级安装（`winget install Gyan.FFmpeg`、Chocolatey，或任何你信任的构建）把它装进
系统 `PATH`——程序同样按 `PATH` 查找，两种放法等效。

自动下载使用配置中的固定哈希或签名，不再记录首次信任旁路文件。校验失败时请手动安装，或由维护者核对上游发布后更新资源配置。

> full 包已内置 FFmpeg 和 Node.js。lite / 源码运行缺少组件时直接下载到程序目录；单文件工具需自行提供运行时。
> Docker 使用镜像内的组件，不执行自动下载。macOS 的 FFmpeg 归档使用 GPG 验签；没有 gpg 时可手动安装 FFmpeg。
> Apple Silicon 仍优先使用系统已有的原生 FFmpeg。

## 🎈 已支持平台

**国内站点（37 个）**：抖音 | 快手 | 虎牙 | 斗鱼 | YY | B站 | 小红书 | bigo | blued | 网易CC | 千度热播 | 猫耳FM | Look直播 | TwitCasting | 百度 | 微博 | 酷狗 | 花椒 | 流星 | Acfun | 畅聊 | 映客 | 音播 | 知乎 | 嗨秀 | VV星球 | 17Live | 浪Live | 飘飘 | 六间房 | 乐嗨 | 花猫 | 淘宝 | 京东 | 咪咕 | 连接 | 来秀

**海外站点（14 个）**：TikTok | SOOP(原AfreecaTV) | PandaTV | WinkTV | TTingLive(原Flextv) | PopkonTV | TwitchTV | LiveMe | ShowRoom | CHZZK | Shopee | YouTube | Faceit | Picarto

> 合计 **51 个**平台（对外标称 60+，含持续添加中的平台）。各平台数据获取函数位于 `src/streams/spider.py`，流地址解析位于 `src/streams/stream.py`。

**弹幕录制支持（5 个平台）**：抖音直播 | 斗鱼直播 | 虎牙直播 | B站直播 | TwitchTV

**实际画质回采与降级告警（7 个平台）**：抖音 | TikTok | 快手 | 虎牙 | 斗鱼 | B站 | 网易CC

**斗鱼支持的链接形态（两种域名走同一套录制流程）**：`https://www.douyu.com/8751648` | `https://m.douyu.com/8751648`（移动端）。尾斜杠（`.../8751648/`）、查询参数（`...?rid=8751648`、`...?dyshid=...`）、`http://` 协议与附加路径均可识别；字母号与分享链接会先还原为数字房间号再取流。链接缺少房间号或房间号非法时给出明确错误提示（链接入日志前一律脱敏）。

**B站支持的链接形态（两种域名走同一套录制流程）**：`https://live.bilibili.com/22747736` | `https://b23.tv/22747736`（移动端分享短域，纯数字房间号；尾斜杠、查询参数与 `http://` 协议均可识别）。字母短码（如 `https://b23.tv/ZyQrgYf`）会先跟随 302 跳转解析出真实房间号（结果进程内缓存，不重复请求）再取流；跳转落地不是B站直播间、或链接缺少房间号 / 房间号非法时给出明确错误提示（链接入日志前一律脱敏）。

**虎牙支持的链接形态（移动端分享短链与桌面链接走同一套录制流程）**：`https://www.huya.com/30764624` | `https://hy.fan/30764624`（移动端分享域，纯数字路径段即房间号；尾斜杠、查询参数与 `http://` 协议均可识别）。字母短码（如 `https://hy.fan/JbmwoV`）会先跟随 301 跳转解析出真实房间段（结果进程内缓存，不重复请求）再归一为桌面链接取流；跳转落地不是虎牙直播间、或链接缺少房间号 / 房间号非法时给出明确错误提示（链接入日志前一律脱敏）。

## 📁 项目结构

运行时代码按职责组织在 `src/` 下。唯一入口为 `src/main.py`，按 `[Web] web_enable` 决定是否加载 `src/web.py` 的面板，两者共用 `src/recording/engine.py` 录制引擎。

```text
DouyinLiveRecorder/
├── src/
│   ├── __init__.py                # 显式 Node 初始化与弹幕工厂
│   ├── main.py / web.py           # 统一入口 / 面板实现
│   ├── app/                       # 启动清理、退出协调与日志归档
│   ├── config/                    # INI 解析、读写、布尔值与配置备份
│   ├── recording/                 # 录制引擎、房间会话、状态、调度、进程与后处理
│   ├── streams/                   # 平台分派、数据获取、流地址/画质解析、选源与签名
│   ├── danmaku/                   # 弹幕采集、字幕、监控、解压与 platforms/ 协议实现
│   ├── monitors/                  # 账号关注与私信监控
│   ├── notifications/             # 消息推送、录制反馈与自定义脚本
│   ├── network/                   # HTTP、WebSocket、代理、凭据缓存与目标校验
│   ├── runtime/                   # FFmpeg/Node 安装、资源校验与 JS 执行
│   ├── api/                       # Web 路由、请求模型与面板配置
│   ├── support/                   # 日志、国际化、终端与通用工具
│   ├── proto/                     # 抖音弹幕协议定义、生成代码与类型存根
│   ├── javascript/                # 平台签名资源
│   └── external_resources.json    # 外部资源下载与完整性元数据
├── web/                       # Web 面板静态资源
├── i18n/en_US.json            # 英文翻译目录，中文使用源码原文
├── tests/                     # Python 回归、真机采集脚本与 frontend/ 检查
├── tool/                      # 构建、覆盖率、翻译、冒烟与独立录制工具
├── typings/                   # 第三方类型存根
├── docs/                      # 架构参考、日志归档与历史审查
├── .agents/skills/            # 入库的项目技能
├── .github/                   # CI、发布、镜像工作流与协作模板
├── pyproject.toml             # 版本、依赖与工具配置
├── uv.lock                    # 锁定依赖
├── Dockerfile                 # 多阶段镜像构建
├── docker-compose.yaml        # CLI / Web 服务编排
└── index.html                 # 独立 M3U8 播放器
```

运行期目录按需创建：`config/` 保存 `config.toml` 与 `URL_config.ini`，`downloads/`、`logs/`、`backup_config/` 保存录制、日志与备份，`ffmpeg/`、`node/` 保存本地运行时。根目录 `config/` 是用户数据，`src/config/` 是配置处理代码。

完整文件与职责清单见[目录参考](docs/agent-reference/project-structure.md)，实现细节见[架构文档](CODE_WIKI.md#核心模块详解)，工程约定见 [AGENTS.md](AGENTS.md)。

## ⚙️ 配置说明

### 基础配置 (config/config.toml)

> 配置为 TOML：含中文/括号/`|` 的节名与键名用双引号包裹，布尔写 `true`/`false`。旧版 `config.ini` 在首次启动时自动迁移为 `config.toml`（原文件改名为 `config.ini.migrated`）；读取时按类型校验，非法值回退默认并记警告；文件改动在下一轮检测循环热加载，无需重启。
>
> 完整键位模板见 [config/config.toml.example](config/config.toml.example)，复制为 `config/config.toml` 后填写；该文件含凭据，切勿提交。

```toml
["录制设置"]
# 界面语言：zh_CN | en_US（留空使用中文；英文目录缺失时回退中文）
"language" = ""
# 是否跳过代理检测(是/否)
"是否跳过代理检测(是/否)" = true
# 是否启用日志文件(是/否)
"是否启用日志文件(是/否)" = true
# 启动时清理陈旧字节码缓存：仅当版本号或源码内容变化时清一次（源码没变则零动作）；
# 范围只有程序目录与 src/ 下的 __pycache__，不含 tests/ tool/ downloads/ 等目录
"是否启动时清理陈旧字节码缓存(是/否)" = true
# 直播保存路径(不填则默认 downloads/)
"直播保存路径(不填则默认)" = ""
# 主播改名时自动更新 URL_config.ini 并同步重命名录制目录/文件（默认 是）
"是否自动更新主播名(是/否)" = true
# 保存文件夹是否以作者区分
"保存文件夹是否以作者区分" = true
# 保存文件夹是否以时间区分
"保存文件夹是否以时间区分" = false
# 保存文件夹是否以标题区分
"保存文件夹是否以标题区分" = false
# 保存文件名是否包含标题
"保存文件名是否包含标题" = false
# 是否去除名称中的表情符号
"是否去除名称中的表情符号" = true
# 视频保存格式 ts|mkv|flv|mp4|mp3音频|m4a音频
"视频保存格式ts|mkv|flv|mp4|mp3音频|m4a音频" = "ts"
# 录制画质 原画|超清|高清|标清|流畅
"原画|超清|高清|标清|流畅" = "原画"
# 自定义画质选项(逗号分隔) - 用户自选画质子集，留空或全部非法时回退引擎内置全集（默认 空）；
# Web 端增删画质写回本项，选非默认画质按「画质,直播间地址」写回 config/URL_config.ini
"自定义画质选项(逗号分隔)" = ""
# 是否使用代理ip(是/否)
"是否使用代理ip(是/否)" = false
# 代理地址
"代理地址" = ""
# 同一时间访问网络的线程数
"同一时间访问网络的线程数" = 3
# 最大同时录制数 - 全局并发录制上限，0 为不限制（默认 0）；改值后下一轮检测循环生效
"最大同时录制数(0为不限制)" = 0
# 循环时间(秒) - 直播状态检测间隔（默认 60）
"循环时间(秒)" = 60
# 排队读取网址时间(秒)
"排队读取网址时间(秒)" = 0
# 是否显示循环秒数
"是否显示循环秒数" = false
# 是否显示直播源地址
"是否显示直播源地址" = false
# 分段录制是否开启
"分段录制是否开启" = true
# 是否启用HLS采集(是/否) - 关闭则只走 FLV 等非 HLS 候选
"是否启用HLS采集(是/否)" = true
# HLS采集排除平台(逗号分隔) - 命中平台无视「是否启用HLS采集」配置、恒按 FLV 采集
# （平台名须与日志/配置中显示的完全一致，如：斗鱼直播,虎牙直播；留空不排除任何平台）
"HLS采集排除平台(逗号分隔)" = "虎牙直播"
# 是否启用https录制 - 已整合原「是否强制启用https录制」与「是否禁用SSL证书验证(是/否)」：
# 开启 = 流地址以 https 拉流并跳过 SSL 证书校验；关闭 = 流地址以 http 拉流并恢复默认证书校验
# （旧键「是否强制启用https录制」的值会自动迁移继承；TikTok/YouTube 等 https-only 海外平台在关闭时保持原样）
"是否启用https录制" = true
# 禁用SSL证书验证的平台(逗号分隔) - 仅在「是否启用https录制 = 否」（http 模式、需证书校验）时生效。
# FFmpeg 9.0 起 TLS 证书验证默认开启，证书异常平台需在此豁免；启动时会自动追加必需平台
# （虎牙直播 / B站直播），只追加不移除用户手填项
"禁用SSL证书验证的平台(逗号分隔)" = "虎牙直播,B站直播"
# 录制空间剩余阈值(gb)
"录制空间剩余阈值(gb)" = 1.0
# 视频分段时间(秒)（默认 1800）
"视频分段时间(秒)" = 1800
# 录制完成后自动转为mp4格式
"录制完成后自动转为mp4格式" = false
# mp4格式重新编码为h264
"mp4格式重新编码为h264" = false
# 追加格式后删除原文件
"追加格式后删除原文件" = true
# 生成时间字幕文件
"生成时间字幕文件" = false
# 是否录制完成后执行自定义脚本
"是否录制完成后执行自定义脚本" = false
# 自定义脚本执行命令
"自定义脚本执行命令" = ""
# 使用代理录制的平台(逗号分隔)
"使用代理录制的平台(逗号分隔)" = "tiktok, sooplive, pandalive, winktv, flextv, popkontv, twitch, liveme, showroom, chzzk, shopee, shp, youtu, faceit"
# 额外使用代理录制的平台(逗号分隔)
"额外使用代理录制的平台(逗号分隔)" = ""
# 是否录制弹幕(是/否) - 开启后弹幕落为 SRT 字幕文件，与视频录制同起同停
"是否录制弹幕(是/否)" = true
# 是否弹幕监控(是/否) - 仅实时查看弹幕、不写 SRT（与弹幕录制解耦，可单独开启）
"是否弹幕监控(是/否)" = true
# 弹幕分片时长(秒) - SRT 分片粒度，建议与「视频分段时间(秒)」一致
"弹幕分片时长(秒)" = 1800
# 弹幕录制平台(逗号分隔) - 目前支持的 5 个平台
"弹幕录制平台(逗号分隔)" = "斗鱼直播,B站直播,虎牙直播,抖音直播,TwitchTV"
# 单次录制时长上限（秒）；0 表示不限制
"单次录制时长上限(秒,0为不限制)" = 0```

### 推送配置 (config/config.toml)

```toml
["推送配置"]
# 可选微信|钉钉|tg|邮箱|bark|ntfy|pushplus 可填多个
"直播状态推送渠道" = ""
"钉钉推送接口链接" = ""
"微信推送接口链接" = ""
"bark推送接口链接" = ""
"bark推送中断级别" = "active"
"bark推送铃声" = "bell"
"钉钉通知@对象(填手机号)" = ""
"钉钉通知@全体(是/否)" = false
"tgapi令牌" = ""
"tg聊天id(个人或者群组id)" = ""
"smtp邮件服务器" = ""
"是否使用smtp服务ssl加密(是/否)" = true
"smtp邮件服务器端口" = ""
"邮箱登录账号" = ""
"发件人密码(授权码)" = ""
"发件人邮箱" = ""
"发件人显示昵称" = ""
"收件人邮箱" = ""
"ntfy推送地址" = ""
"ntfy推送标签" = "tada"
"ntfy推送邮箱" = ""
"pushplus推送token" = ""
"自定义推送标题" = "直播间状态更新通知"
"自定义开播推送内容" = ""
"自定义关播推送内容" = ""
"只推送通知不录制(是/否)" = false
"直播推送检测频率(秒)" = 1800
"开播推送开启(是/否)" = true
"关播推送开启(是/否)" = false```

### Cookie 配置 (config/config.toml)

```toml
["Cookie"]
# 录制抖音必填：请填入从浏览器 live.douyin.com 复制的有效 cookie（至少包含 ttwid）
# 留空将自动尝试获取访客 ttwid（可能触发风控，建议填写）
"抖音cookie" = ""
# 单独指定抖音 ttwid（留空则由 src/streams/ttwid.py 自动获取并进程级缓存）
"ttwid" = ""
"快手cookie" = ""
"tiktok_cookie" = ""
"tiktok_guest_cookie" = ""
"虎牙cookie" = ""
"斗鱼cookie" = ""
"yy_cookie" = ""
"b站cookie" = ""
"小红书cookie" = ""
# 小红书 app 端接口的会话 sid（xy-common-params 头），留空则用内置缺省（可能已过期）
# 优先级：环境变量 XHS_SESSION_SID > 本键 > 内置缺省
"xhs_session_sid" = ""
"bigo_cookie" = ""
# ... 共 51 个平台 cookie 键，其余详见 config.toml```

> 访客类 cookie（抖音 ttwid、快手 did 等）由 `src/network/credentials.py` 以「归一化网址 + 代理」为 key 做进程级共享缓存（默认 30 分钟 TTL），多直播间并发时不会重复拉取触发风控。

### 授权配置 (config/config.toml)

```toml
["Authorization"]
# PopkonTV 登录后取得的 token（由账号密码自动登录后写回）
"popkontv_token" = ""```

### 账号密码配置 (config/config.toml)

```toml
["账号密码"]
"sooplive账号" = ""
"sooplive密码" = ""
"flextv账号" = ""
"flextv密码" = ""
"popkontv账号" = ""
"partner_code" = "P-00001"
"popkontv密码" = ""
"twitcasting账号类型" = "normal"
"twitcasting账号" = ""
"twitcasting密码" = ""```

### Web 管理面板配置 (config/config.toml)

```toml
["Web"]
# 是否随唯一入口启动 Web 管理面板（false = 纯命令行录制，不监听端口）
"web_enable" = false
# Web 管理面板监听地址（默认仅本机；Docker/LAN 部署再显式改为 0.0.0.0）
"web_host" = "127.0.0.1"
# Web 管理面板端口
"web_port" = 8000
# 是否启用密码登录（true/false）
"web_auth_enable" = "false"
# 访问密码（启用认证时必填）
"web_password" = ""
# token 有效期（秒）
"web_token_expiry" = 86400
# 是否显示控制台窗口（false 时后台运行，日志写入 logs/web_console.log）
"web_show_console" = "true"
# 受信任的反向代理来源（逗号分隔）。留空时不解析 X-Forwarded-For；
# 置于 Nginx/Caddy 之后时填入代理 IP，登录限流才能取到真实客户端 IP
"web_trusted_proxy" = ""
# 允许的 Host / Origin 白名单（逗号分隔）。仅在把 web_host 绑到 0.0.0.0/:: 且通过域名访问时需要；
# 未列出的多级域名 Host 会被拒绝，用于阻断 DNS 重绑定（attacker.tld → 127.0.0.1）
"web_allowed_hosts" = ""```

> `web_enable` 默认 `false`（纯命令行录制，不监听端口）；Docker 与本地运行一致，只由 config.toml 决定，compose 不再注入覆盖变量。8000 端口恒定映射，未开面板时容器内无人监听。
>
> `web_host` 在仓库默认配置中为 `127.0.0.1`（仅本机可访问）。Docker 或需局域网/公网访问时改为 `0.0.0.0`，并务必同时开启 `web_auth_enable` 与 `web_password`。

### 直播间配置 (config/URL_config.ini)

抖音支持以下 5 种直播间/主页地址格式（其余平台格式见下方各平台示例）：

```
# 1) 网页端主播直播间（数字房间号）
https://live.douyin.com/745964462470

# 2) app端主播直播间（分享短链）
https://v.douyin.com/iQFeBnt/

# 3) 抖音号拼接（https://live.douyin.com/ + 抖音号，支持 VR 直播录制）
https://live.douyin.com/yall1102

# 4) app端主播主页（分享短链）
https://v.douyin.com/CeiU5cbX

# 5) 网页端主播主页（用户页地址）
https://www.douyin.com/user/MS4wLjABAAAA3kr2yA4aRD-sjf9cx8xkOH8Di3RjktpKcAvqIetpsF0
```

> 说明：
> - 格式 1/3/5 走网页端接口（支持 VR 直播）；格式 2/4 走 app 端接口
> - 格式 5（网页端主播主页 `www.douyin.com/user/<sec_uid>`）会直接从地址提取 `sec_user_id` 并解析出抖音号，按直播间地址走网页端接口录制，无需经过 app 端探测
> - 格式 4（app 端主页）等短链形态会先探测直播间地址，失败则自动回退到抖音号解析、再走网页端接口录制

```ini
# 指定画质（画质,直播间地址）
超清，https://live.douyin.com/745964462470

# 指定画质和主播名（画质,直播间地址,主播:名称）
高清，https://live.bilibili.com/123456，主播: B站主播

# 注释直播间（在地址前加 #）
# https://live.douyin.com/123456789```

### 环境变量配置

| 变量名 | 说明 | 示例 |
| --- | --- | --- |
| `PYTHONUNBUFFERED` | 实时输出日志 | `1` |
| `PYTHONDONTWRITEBYTECODE` | 不生成 .pyc 文件 | `1` |
| `PYTHONIOENCODING` | Python 输出编码 | `utf-8` |
| `TZ` | 时区设置 | `Asia/Shanghai` |
| `TERM` | 终端类型 | `xterm-256color` |

## 🎬 使用说明

### 启动（唯一入口）

```bash
python -m src.main
```

是否同时启动 Web 管理面板由 `config/config.toml` 的 `[Web] web_enable` 决定（默认 `false`，即纯命令行录制）。设为 `true` 后，同一条命令会额外监听面板端口，浏览器访问 `http://localhost:8000`。`python -m src.web` 不再是启动方式（`src/web.py` 只是面板实现模块）。

功能：
- **仪表盘**：实时查看监测/录制数、错误数、磁盘剩余、录制中列表、日志流（SSE 推送）
- **直播间管理**：在线增删改查直播间地址、启用/禁用（自动热加载到录制主循环）
- **配置编辑**：在线编辑 config.toml 各项（录制设置/推送/Cookie 等），敏感配置项脱敏
- **文件浏览**：浏览 downloads 目录并下载录制文件
- **实际画质展示**：录制表格显示"设置画质/实际画质"，降级时标红高亮
- **弹幕查看**：读取弹幕监控枢纽快照，实时展示各房间弹幕事件
- **语言切换**：顶栏语言选择器，中英文即时切换（写回配置并热切换进程内翻译，无需重启）

主要 API 路由（均需 Token，认证关闭时除外）：

| 路由 | 方法 | 功能 |
| --- | --- | --- |
| `/api/login` | POST | 密码登录，返回 Token |
| `/api/status` | GET | 录制状态（含实际画质） |
| `/api/status/stream` | GET | 录制状态 SSE 流 |
| `/api/rooms` | GET/POST/PUT/DELETE | 直播间增删改查 |
| `/api/rooms/toggle` | POST | 启用 / 禁用直播间 |
| `/api/config` | GET/PUT | 读取 / 修改配置 |
| `/api/language` | GET/PUT | 查询 / 切换界面语言 |
| `/api/files`、`/api/files/download` | GET | 录制文件浏览与下载 |
| `/api/logs`、`/api/logs/stream` | GET | 日志查询 / SSE 实时推送 |
| `/api/danmaku` | GET | 弹幕监控快照 |

Web 模式与命令行模式共用同一录制引擎与配置文件，直播间地址的增删改会自动被录制主循环热加载。

后台运行模式：将 `web_show_console` 设为 `false`，Windows 下会隐藏控制台窗口，日志写入 `logs/web_console.log`，程序完全后台运行。


> ⚠️ **安全提示**：默认仅监听 `127.0.0.1` 且未启用认证；若为 Docker / 局域网 / 公网访问改绑 `0.0.0.0`，必须同时开启 `web_auth_enable` 并设置强密码。

### 录制格式推荐

- **长时间录制**：推荐使用 `ts` 格式，实时写入，断电不易损坏
- **短时间录制**：推荐使用 `mp4` 或 `mkv` 格式，录制完成后直接可用
- **仅音频录制**：推荐使用 `mp3` 或 `m4a` 格式

### 画质说明

| 画质代码 | 中文名 | 说明 |
| --- | --- | --- |
| OD | 原画 | Original Definition，最高画质 |
| BD | 蓝光 | Blu-ray，超高清 |
| UHD | 超清 | Ultra HD |
| HD | 高清 | High Definition |
| SD | 标清 | Standard Definition |
| LD | 流畅 | Low Definition，最低画质 |

支持平台：抖音、TikTok、快手、虎牙、斗鱼、B站、网易CC。当平台实际下发画质低于设置画质时，会自动告警并标记。

### 采集方式选择（HLS/FLV）

录制拉流源在 HLS（m3u8，逐段拉取）与 FLV（单条长连接）之间自动选择：默认优先 HLS、校验不可达时按序回退 FLV，最后兜底 `record_url`。由两个配置项共同控制：

| 配置项 | 作用 |
| --- | --- |
| `是否启用HLS采集(是/否)` | 全局开关：开启（默认）时优先使用 HLS 源，不可达或源不存在时回退 FLV；关闭则所有平台只走 FLV 等非 HLS 候选 |
| `HLS采集排除平台(逗号分隔)` | 平台级排除列表：命中平台**无视全局开关、恒按 FLV 采集**（等效于仅对该平台关闭 HLS 采集，HLS 源不参与候选与回退） |

- **适用场景**：整体偏好 HLS 录制（HLS 逐段拉取免疫单条长连接被 CDN 掐断），但个别平台的 HLS 源不稳定或被风控，希望仅该平台强制走 FLV 时，将其加入排除列表即可——无需为此关闭全局 HLS 开关
- **填写格式**：平台名须与日志/配置文件中显示的**完全一致**（如 `斗鱼直播`，不能简写为 `斗鱼`）；多个平台逗号分隔（中英文逗号均可），如 `斗鱼直播,虎牙直播`；留空（默认）不排除任何平台，行为与旧版本完全一致
- **优先级语义**：排除列表优先于全局开关——即使「是否启用HLS采集(是/否) = 是」，命中平台也只走 FLV；列表外平台不受任何影响，仍按全局配置 HLS 优先
- **边界行为**：排除平台若解析结果仅有 HLS 源且无 FLV/record_url 可回退，本轮放弃录制并输出告警（日志会提示「可将该平台移出排除列表恢复 HLS 采集」）；h265 编码的 FLV 源不再触发切换 HLS（h265 FLV 录制自动改存 TS 格式的逻辑不受影响）
- **热更新**：主循环每轮重读配置，修改保存后无需重启程序，下一轮监测即生效

### 弹幕录制与弹幕监控

弹幕功能由两个**相互解耦**的开关控制，可单独或同时开启：

| 配置项 | 作用 |
| --- | --- |
| `是否录制弹幕(是/否)` | 弹幕落盘为 SRT 字幕文件，与视频录制同起同停 |
| `是否弹幕监控(是/否)` | 仅实时查看弹幕，不写 SRT（GUI「弹幕监控」页 / Web `/api/danmaku`） |
| `弹幕分片时长(秒)` | SRT 分片粒度，建议与「视频分段时间(秒)」一致 |
| `弹幕录制平台(逗号分隔)` | 启用弹幕的平台白名单 |

- **支持平台（5 个）**：抖音直播、斗鱼直播、虎牙直播、B站直播、TwitchTV
- **输出文件**：SRT 与视频分片一一对应，命名为 `{基础名}_{分片序号:03d}.srt`（如 `_000.srt` 对应 `_000.ts`）；未分段时为 `{基础名}.srt`。时间轴以单调时钟为基准，与 ffmpeg `segment -reset_timestamps` 的 PTS 对齐，可直接被播放器加载
- **弹幕连接直连**：弹幕 WebSocket 显式不跟随系统代理，避免 SOCKS 代理环境下连接即断
- **监控边车日志**：弹幕监控事件同时写入 `logs/danmaku_monitor.jsonl`（5MB 轮转）
- 抖音弹幕在 Cookie 为空时会自动获取访客 ttwid；B站弹幕会自动获取真实 buvid（登录 cookie → spi 接口 → 首页 Set-Cookie → 随机兜底）

### 主播名自动更新

开启 `是否自动更新主播名(是/否)`（默认「是」）后，程序在每轮解析到最新主播名时，若与 `URL_config.ini` 中记录的名称不同，会自动完成两件事：

1. **重命名文件系统**：`{保存路径}/{平台}/{旧主播名}` → 新主播名目录；递归重命名目录树内所有以 `{旧名}_` 开头的录制产物（TS / FLV / SRT / 字幕）及以 `_{旧名}` 结尾的标题目录；若新名目录已存在则逐项合并移入（兼容主播改回曾用名）
2. **回写配置文件**：按 URL 段级精确匹配只替换该行的主播名字段，完整保留画质段、`#` 注释前缀与行尾换行风格；操作幂等

安全约束：

- 检测点位于「解析直播数据之后、录制启动之前」，此时该线程必然不在录制中，天然避开 ffmpeg 文件占用窗口
- **先文件系统、后配置文件，两者全部成功才切换本轮使用名**；任一失败则保持旧名并在下轮轮询自动重试
- 个别文件被后台转码/播放器占用时仅告警跳过，不阻塞整体
- 自动跳过自定义流地址（其主播名含每轮随机 UUID）与清洗后为空白的昵称
- 关闭该开关即保持手动名称不变

### 多语言与界面切换

提供中文和英文，默认中文；仅英文加载 `i18n/en_US.json`：

| 语言码 | 显示名 | 目录文件 |
| --- | --- | --- |
| `zh_CN` | 中文 | 源码原文，无需翻译文件 |
| `en_US` | English (US) | `i18n/en_US.json` |

- 配置键 `language`：留空使用中文；`en` / `en-US` / `en-GB` 归一为英文，中文地区码归一为中文；未知值或英文目录不可用时回退中文。
- **热切换**：Web 面板顶栏语言选择器、直接编辑 `config.toml` 两种途径均可切换；命令行主循环每轮检测配置变化并即时重载翻译，**无需重启进程**（录制中的 ffmpeg 子进程不受影响）
- 翻译不再依赖 `LANG` / `LANGUAGE` 环境变量（Windows 普遍未设置）
- 只需维护 `i18n/en_US.json`，中文直接显示源码原文，无需编译或额外校验脚本。

### 停止录制

- **Windows**：在命令行按 `Ctrl+C`
- **Linux/macOS**：在命令行按 `Ctrl+C`
- **Docker**：执行 `docker-compose stop`

### 注意事项

1. 如需录制 TikTok、SOOP(原AfreecaTV) 等海外平台，请在配置中开启代理
2. 长时间挂机建议将循环时间设置长一些（如 60 秒），避免请求频繁被封 IP
3. 直播结束后会自动保存文件，无需手动停止
4. 如遇录制的视频文件损坏，建议使用 `ts` 格式录制
5. 录制抖音需要填写有效的 cookie（至少包含 ttwid），否则可能触发风控
6. 部分平台需要 Node.js 环境运行 JavaScript 签名脚本，Windows 下会自动安装

## 🐋 Docker 部署

### 前置要求

- 已安装 [Docker](https://docs.docker.com/get-docker/)
- 已安装 [Docker Compose](https://docs.docker.com/compose/install/)

### 快速启动

```bash
# 1. 克隆项目
git clone https://github.com/y123ao6/DouyinLiveRecorder.git
cd DouyinLiveRecorder

# 2. 编辑配置文件
# 在 config/URL_config.ini 中添加直播间地址

# 3. 启动容器（默认命令行录制模式）
docker compose up -d

# 4. 查看日志
docker compose logs -f
```

### 拉取预构建的多架构镜像（GHCR）

发布工作流 `.github/workflows/docker-publish.yml` 在每次推 `v*` tag 时，由原生 amd64 与 arm64 两个 runner
各构建并推送一层，再按 manifest digest 合成多架构清单——**同一条 `docker pull` 在 x86_64 与 arm64 主机上
都会拿到本机架构那一层**：

```bash
# <owner> 换成实际持有该镜像的 GitHub 账号名（GHCR 命名空间一律小写）
docker pull ghcr.io/<owner>/douyin-live-recorder:latest
docker run -d -v ./config:/app/config -v ./downloads:/app/downloads \
  -v ./logs:/app/logs -v ./backup_config:/app/backup_config \
  ghcr.io/<owner>/douyin-live-recorder:latest
```

- 标签形态：`latest` 与 `vX.Y.Z`；`latest` 只随 `v*` tag 产出，不开放手动触发覆盖。
  两个分架构 tag（`:vX.Y.Z-amd64` / `-arm64`）属合成前的中间产物，当前不会自动清理。
- 匿名拉取要求该 GHCR 包的可见性为 **public**——那是仓库设置层面的一次性人工动作。
- 与 `docker-compose.yaml` 里的 `image: y123ao6/douyin-live-recorder:latest` **刻意不同源**：compose 走
  `pull_policy: build` 就地构建（不拉 registry 上的同名镜像），上面「快速启动」的说明保持不变；
  要用 GHCR 预构建镜像请按上面的 `docker run`，arm64 主机走 compose 就地构建同样可行。

### 运行模式

`docker-compose.yaml` 只有一个服务、唯一入口 `python -m src.main`；是否同时开面板由 `config/config.toml` 的 `[Web] web_enable` 决定：

```bash
# 纯录制（默认；web_enable = false）
docker compose up -d

# 面板 + 录制：把 web_enable 设为 true 后，同一命令启动，浏览器访问 http://localhost:8000
```

> Docker 仅支持 CLI / Web。

> ⚠️ Web 模式注意：面板默认监听 `127.0.0.1`，容器内必须在 `config/config.toml`
> 的 `[Web]` 节设置 `web_host = 0.0.0.0` 才能从宿主机访问；同时建议开启
> `web_auth_enable = true` 并配置 `web_password`。

### 数据挂载

```yaml
volumes:
  - ./config:/app/config:rw          # 配置文件目录（必需）
  - ./downloads:/app/downloads:rw    # 录制文件下载目录（必需）
  - ./logs:/app/logs:rw              # 运行日志目录
  - ./backup_config:/app/backup_config:rw  # 配置备份目录
```

### 端口映射

唯一服务恒定映射面板端口；`web_enable = false` 时容器内无人监听（连接被拒，不影响录制）：

```yaml
ports:
  - "127.0.0.1:8000:8000"   # Web 管理面板端口（仅 web_enable = true 时实际监听，默认只暴露给宿主机本地）
```

### 环境变量

> 与上文「环境变量配置」同一张表（`TZ` / `PYTHONUNBUFFERED` / `PYTHONDONTWRITEBYTECODE` / `PYTHONIOENCODING` / `TERM`），compose 已按该表注入，此处不重复列举。

### Docker 镜像特性

- **多阶段构建**：builder 阶段安装依赖到虚拟环境，runtime 阶段精简镜像
- **非 root 用户**：使用 `recorder` 用户运行，提升安全性
- **健康检查**：自动检测 `python -m src.main` 进程是否存活（面板模式进程名相同）
- **资源限制**：默认限制 2 CPU / 2G 内存（可在 docker-compose.yaml 调整）
- **日志轮转**：单文件 50MB，最多保留 3 份
- **内置 Node.js 24 LTS**：用于运行 JavaScript 签名脚本

## ❤️ 贡献者

<a href="https://github.com/y123ao6/DouyinLiveRecorder/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=y123ao6/DouyinLiveRecorder" />
</a>

## 📄 许可证

本项目基于 [MIT License](LICENSE) 开源，欢迎 Star 和 Fork！

## 💬 有问题或者需求可以向我提 Issue，欢迎 Star 与 Fork

[![Star History Chart](https://api.star-history.com/svg?repos=y123ao6/DouyinLiveRecorder&type=Timeline)](https://star-history.com/#y123ao6/DouyinLiveRecorder&Timeline)
