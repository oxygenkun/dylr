![video_spider](https://socialify.git.ci/oxygenkun/dylr/image?font=Inter&forks=1&language=1&owner=1&pattern=Circuit%20Board&stargazers=1&theme=Light)

[文档导航](docs/README.md) · [配置说明](docs/configuration.md) · [使用说明](docs/usage.md) · [部署](docs/deployment.md) · [常见问题（Q & A）](docs/faq.md) · [更新记录](docs/changelog/release-notes-zh.md)

## 💡 简介

[![Python Version](https://img.shields.io/badge/python-v3.14%2B-blue?logo=python)](https://www.python.org/downloads/)
[![Node.js Version](https://img.shields.io/badge/node.js-LTS_v24%2B-blue?logo=node.js)](https://nodejs.org/en/download)
[![FFmpeg Version](https://img.shields.io/badge/ffmpeg-v9.0%2B-blue?logo=ffmpeg)](https://www.ffmpeg.org/download.html)
[![Supported Platforms](https://img.shields.io/badge/platforms-Windows%7CLinux%7CMacOS-blue)](https://github.com/oxygenkun/dylr)
[![GitHub issues](https://img.shields.io/github/issues/oxygenkun/dylr)](https://github.com/oxygenkun/dylr/issues)
[![Latest Release](https://img.shields.io/github/v/release/oxygenkun/dylr)](https://github.com/oxygenkun/dylr/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/oxygenkun/dylr/total)](https://github.com/oxygenkun/dylr/releases/latest)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Stars](https://img.shields.io/github/stars/oxygenkun/dylr)](https://github.com/oxygenkun/dylr/stargazers)

一款**简易**的可循环值守的直播录制工具，基于 FFmpeg 实现多平台直播源录制，支持自定义配置录制以及直播状态推送。

上游项目：[ihmily/DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder)

维护者：[oxygenkun](https://github.com/oxygenkun)

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

1. 进入 [Releases](https://github.com/oxygenkun/dylr/releases) 下载最新 zip。每个平台提供 lite（不含内置运行时，首启尝试自动获取）与 full（内置 FFmpeg / Node.js）两包；命名规则与平台差异见[部署说明](docs/deployment.md#运行包命名)。
2. 解压后，在 `config` 文件夹的 `URL_config.ini` 中添加直播间地址。
3. 运行 `dylr.exe` 开始录制。

缺少 FFmpeg / Node.js 或自动安装失败时，见[常见问题](docs/faq.md)。

### 方式二：源码运行（推荐开发者）

需要 Python 3.14+ 和 uv。

```bash
git clone https://github.com/oxygenkun/dylr.git
cd dylr
uv sync --locked
# 唯一入口；是否启动 Web 面板由 config.toml 的 [Web] web_enable 决定
uv run --locked python -m src.main
```

### 方式三：Docker 运行

```bash
docker compose up -d
```

面板、镜像拉取、挂载与端口说明见[部署](docs/deployment.md)。

## 🎈 已支持平台

**国内站点（37 个）**：抖音 | 快手 | 虎牙 | 斗鱼 | YY | B站 | 小红书 | bigo | blued | 网易CC | 千度热播 | 猫耳FM | Look直播 | TwitCasting | 百度 | 微博 | 酷狗 | 花椒 | 流星 | Acfun | 畅聊 | 映客 | 音播 | 知乎 | 嗨秀 | VV星球 | 17Live | 浪Live | 飘飘 | 六间房 | 乐嗨 | 花猫 | 淘宝 | 京东 | 咪咕 | 连接 | 来秀

**海外站点（14 个）**：TikTok | SOOP(原AfreecaTV) | PandaTV | WinkTV | TTingLive(原Flextv) | PopkonTV | TwitchTV | LiveMe | ShowRoom | CHZZK | Shopee | YouTube | Faceit | Picarto

> 合计 **51 个**平台（对外标称 60+，含持续添加中的平台）。各平台数据获取函数位于 `src/streams/spider.py`，流地址解析位于 `src/streams/stream.py`。

**弹幕录制支持（5 个平台）**：抖音直播 | 斗鱼直播 | 虎牙直播 | B站直播 | TwitchTV

**实际画质回采与降级告警（7 个平台）**：抖音 | TikTok | 快手 | 虎牙 | 斗鱼 | B站 | 网易CC

各平台链接形态（斗鱼、B站、虎牙短链等）见[使用说明](docs/usage.md#常见平台链接形态)，解析实现见[架构专题](docs/architecture/platforms.md)。

## 📁 项目结构

运行时代码按职责组织在 `src/` 下。唯一入口为 `src/main.py`，按 `[Web] web_enable` 决定是否加载 `src/web.py` 的面板，两者共用 `src/recording/engine.py` 录制引擎。

完整源码树与文件职责见[项目结构参考](docs/agent-reference/project-structure.md)，实现细节见[架构专题](docs/architecture/README.md)，工程约定见 [AGENTS.md](AGENTS.md)。

## ⚙️ 配置

主配置为 `config/config.toml`，直播间列表为 `config/URL_config.ini`。完整键位、各节示例、URL 写法与环境变量见[配置说明](docs/configuration.md)；可复制的模板见 [config/config.toml.example](config/config.toml.example)。

> 配置为 TOML；旧版 `config.ini` 首次启动自动迁移为 `config.toml`。读取时按类型校验，非法值回退默认并记警告；修改在下一轮检测循环热加载，无需重启。

## 🎬 使用

- **启动**：`python -m src.main`；Web 面板由 `[Web] web_enable` 控制（默认纯命令行录制）。
- **录制格式、画质、HLS/FLV、弹幕、多语言、停止方式与注意事项**：见[使用说明](docs/usage.md)。
- **主播名自动更新**：见[实现说明](docs/architecture/configuration.md#主播名自动更新)。
- **Web 接口与安全边界**：见[Web 面板与接口](docs/architecture/web.md)。

## 🐋 Docker 部署

`docker-compose.yaml` 只有一个服务，唯一入口 `python -m src.main`；是否开面板由 `config/config.toml` 的 `[Web] web_enable` 决定。

```bash
docker compose up -d          # 纯录制（默认）
docker compose logs -f        # 查看日志
```

容器使用镜像内的 FFmpeg / Node.js，`config/`、`downloads/`、`logs/`、`backup_config/` 通过卷挂载持久化。面板访问、GHCR 多架构镜像、端口与镜像特性见[部署](docs/deployment.md)。

## ❤️ 贡献者

<a href="https://github.com/oxygenkun/dylr/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=oxygenkun/dylr" />
</a>

## 📄 许可证

本项目基于 [MIT License](LICENSE) 开源，欢迎 Star 和 Fork！

## 💬 有问题或者需求可以向我提 Issue，欢迎 Star 与 Fork

[![Star History Chart](https://api.star-history.com/svg?repos=oxygenkun/dylr&type=Timeline)](https://star-history.com/#oxygenkun/dylr&Timeline)
