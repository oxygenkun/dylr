![video_spider](https://socialify.git.ci/y123ao6/DouyinLiveRecorder/image?font=Inter&forks=1&language=1&owner=1&pattern=Circuit%20Board&stargazers=1&theme=Light)

English&nbsp;&nbsp;|&nbsp;&nbsp;[简体中文](README.md)

[Development Guide](docs/development-en.md) · [FAQ (Q & A)](docs/faq-en.md) · [Changelog](docs/changelog/release-notes-en.md)

## 💡 Introduction

[![Python Version](https://img.shields.io/badge/python-v3.14%2B-blue?logo=python)](https://www.python.org/downloads/)
[![Node.js Version](https://img.shields.io/badge/node.js-LTS_v24%2B-blue?logo=node.js)](https://nodejs.org/en/download)
[![FFmpeg Version](https://img.shields.io/badge/ffmpeg-v9.0%2B-blue?logo=ffmpeg)](https://www.ffmpeg.org/download.html)
[![Supported Platforms](https://img.shields.io/badge/platforms-Windows%7CLinux%7CmacOS-blue)](https://github.com/y123ao6/DouyinLiveRecorder)
[![GitHub issues](https://img.shields.io/github/issues/y123ao6/DouyinLiveRecorder)](https://github.com/y123ao6/DouyinLiveRecorder/issues)
[![Latest Release](https://img.shields.io/github/v/release/y123ao6/DouyinLiveRecorder)](https://github.com/y123ao6/DouyinLiveRecorder/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/y123ao6/DouyinLiveRecorder/total)](https://github.com/y123ao6/DouyinLiveRecorder/releases/latest)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Stars](https://img.shields.io/github/stars/y123ao6/DouyinLiveRecorder)](https://github.com/y123ao6/DouyinLiveRecorder/stargazers)

A **lightweight** loop-monitoring live-stream recording tool that uses FFmpeg to record live sources from multiple platforms, supporting custom recording configuration and live-status notifications.

Upstream project: [ihmily/DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder)

## ✨ Features

| Feature | Description |
| --- | --- |
| 🎯 **Multi-platform support** | Supports 51 platforms including Douyin, TikTok, YouTube, Kuaishou, Huya, Douyu, Bilibili, etc. (more being added) |
| 🔄 **Loop monitoring** | Automatically detects live status — starts recording when live and stops when offline |
| 🎬 **Multiple formats** | Supports TS, MKV, FLV, MP4, MP3, M4A and other output formats |
| 🖥️ **Two run modes** | CLI mode and Web management panel mode |
| 📊 **Quality monitoring** | Real-time detection of each room's actual quality, with automatic alerts on quality degradation |
| 💬 **Danmaku recording** | Captures danmaku from Douyin / Douyu / Huya / Bilibili / TwitchTV, outputting SRT subtitles per segment, synced with video start/stop |
| 👀 **Danmaku monitoring** | Standalone real-time danmaku viewer (not persisted to disk); viewable in the Web panel |
| 🏷️ **Auto anchor-name update** | When an anchor renames, automatically syncs `URL_config.ini` and renames the recording directory and files |
| 📱 **Message push** | Supports DingTalk, WeChat, email, Telegram, Bark, NTFY, PushPlus, etc. |
| 🐳 **Docker support** | Supports Docker containerized deployment, ready to use out of the box |
| 🌐 **Internationalization** | Built-in Chinese / English, with restart-free hot switching in the Web panel |
| ⚙️ **Flexible config** | Per-room customization of quality, format, segmented recording, etc., with hot-reload of config changes |
| 🔐 **Web security** | Token auth, login brute-force rate limiting, path-traversal protection, sensitive-config masking, unauthenticated write protection |

> The native Python GUI and Qt tray have been removed. Use the Web panel or CLI; Tauri packaging is planned separately. Existing `[GUI]` and `web_minimize_to_tray` settings are unused and may be removed manually.

## 🚀 Quick Start

### Method 1: Download the release package (recommended for beginners)

1. Go to [Releases](https://github.com/y123ao6/DouyinLiveRecorder/releases) and download the latest released zip archive.
   Each platform ships two packages — **lite** (no bundled ffmpeg/node; the app tries to fetch them on first start)
   and **full** (runtime bundled). The **platform** segment of the attachment name is produced by
   `build_exe.make_zip()`, which reads `sys.platform` and normalizes it through
   `{"win32": "windows", "darwin": "macos"}` (anything else maps to `linux`); only the **architecture**
   segment is the literal value of `platform.machine().lower()`:

   | Target platform | Attachment name |
   | --- | --- |
   | Windows x64 | `DouyinLiveRecorder-v<version>-windows-amd64-lite.zip` / `-full.zip` |
   | Linux x86_64 | `DouyinLiveRecorder-v<version>-linux-x86_64-lite.zip` / `-full.zip` |
   | **Linux arm64** | `DouyinLiveRecorder-v<version>-linux-aarch64-lite.zip` / `-full.zip` |
   | macOS (Apple Silicon) | `DouyinLiveRecorder-v<version>-macos-arm64-lite.zip` / `-full.zip` |

   > The architecture segment for Linux arm64 reads **`aarch64`** — that is what `platform.machine().lower()` actually
   > returns on that architecture, **not** `arm64`; the package comes out of the four-platform release matrix
   > (`windows` / `linux` / `linux-arm64` / `macos`). On Linux a lite package is fetched in this order:
   > Missing runtimes are downloaded directly using the shared resource manifest; failures require manual installation.
2. After extracting, add live-room URLs to `URL_config.ini` inside the `config` folder
3. Run `DouyinLiveRecorder.exe` to start recording

### Method 2: Run from source (recommended for developers)

Requires Python 3.14+ and uv.

```bash
# Clone the project
git clone https://github.com/y123ao6/DouyinLiveRecorder.git
cd DouyinLiveRecorder

# Install locked dependencies; run the project from source
uv sync --locked

# Run using the project environment (single entry)
# Whether the web panel starts too is controlled by [Web] web_enable in config.toml
uv run --locked python -m src.main

```

### Method 3: Run with Docker

```bash
# Plain recording (default; [Web] web_enable = false in config/config.toml)
docker compose up -d

# Panel + recording: set web_enable = true (and preferably web_auth_enable), start with the
# same command, then open http://localhost:8000 (also set web_host = 0.0.0.0 inside the container)

# Or build locally and start (APP_VERSION is read from pyproject.toml; without it the image LABEL version is baked in as an empty string)
docker build --build-arg APP_VERSION="$(python -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")" -t douyin-live-recorder .
docker run -d -v ./config:/app/config -v ./downloads:/app/downloads -v ./logs:/app/logs -v ./backup_config:/app/backup_config douyin-live-recorder
```

> Inside the container, FFmpeg and Node.js are provided by the image itself (no startup downloads) — no need to mount the local `ffmpeg/`, `node/` directories;
> `config/`, `downloads/`, `logs/`, and `backup_config/` are persisted via volume mounts.
> If you would rather not build locally, a prebuilt multi-architecture image (`linux/amd64` + `linux/arm64`) is published
> on GHCR — see the "Pull the prebuilt multi-arch image (GHCR)" subsection of the "🐋 Docker Deployment" chapter below.

### Installing ffmpeg manually (Windows user guide)

When FFmpeg is missing, Windows downloads the archive configured in `src/external_resources.json` and verifies its integrity. If this fails, install it manually:

1. **Download the official zip**: <https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip>
   To verify it yourself, compare against the official digest document for that same zip,
   <https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip.sha256> (maintainers use it to update the pinned resource manifest).
2. **Take the `bin` folder out of the zip, put it in the app directory and rename it to `ffmpeg`**:

| How you run the app | Required final layout |
| --- | --- |
| Downloaded release package (exe) | `DouyinLiveRecorder\ffmpeg\ffmpeg.exe` |
| From source / single-file script | `<project root>\ffmpeg\ffmpeg.exe` |

   - **Do not** leave it as `ffmpeg\bin\ffmpeg.exe` — the app adds only the `ffmpeg\` directory itself to the
     lookup path and does not search sub-directories.
3. **Verify**: run `ffmpeg -version` inside that `ffmpeg\` directory — a version banner means it works.
   Restart the app; the install took effect once `logs\streamget.log` no longer reports "ffmpeg is not installed.".

Installing system-wide works equally well (`winget install Gyan.FFmpeg`, Chocolatey, or any build you trust) —
the app also looks the binary up on `PATH`.

Automatic downloads use fixed hashes or signatures from the resource manifest. They no longer record TOFU sidecars. If verification fails, install manually or ask the maintainer to verify the upstream release and update the manifest.

> Full packages bundle FFmpeg and Node.js. Lite packages and source runs download missing components directly into the app directory; the standalone tool requires user-provided runtimes.
> Docker uses image-provided components and never auto-downloads them. Automatic FFmpeg installation on macOS requires gpg; users without it can install FFmpeg manually.
> Apple Silicon still prefers an existing native system FFmpeg.

## 🎈 Supported Platforms

**Domestic sites (37)**: Douyin | Kuaishou | Huya | Douyu | YY | Bilibili | Xiaohongshu | bigo | blued | NetEase CC | Qiandu Rebo | MaoerFM | Look Live | TwitCasting | Baidu | Weibo | Kugou | Huajiao | Liuxing | Acfun | Changliao | Inke | Yinbo | Zhihu | Haixiu | VV Planet | 17Live | LangLive | Piaopiao | 6Rooms | Lehai | Huamao | Taobao | JD | Migu | Lianjie | Laixiu

**Overseas sites (14)**: TikTok | SOOP (formerly AfreecaTV) | PandaTV | WinkTV | TTingLive (formerly Flextv) | PopkonTV | TwitchTV | LiveMe | ShowRoom | CHZZK | Shopee | YouTube | Faceit | Picarto

**Douyu link formats (both domains share one recording pipeline)**: `https://www.douyu.com/8751648` | `https://m.douyu.com/8751648` (mobile). Trailing slashes (`.../8751648/`), query strings (`...?rid=8751648`, `...?dyshid=...`), the `http://` scheme and extra path segments are all accepted; alphanumeric room ids and share links are resolved back to the numeric room id before pulling the stream. A link with no room id, or an invalid one, produces an explicit error message (links are masked before they reach the logs).

**Bilibili link formats (both domains share one recording pipeline)**: `https://live.bilibili.com/22747736` | `https://b23.tv/22747736` (mobile share domain, numeric room id; trailing slashes, query strings and the `http://` scheme are all accepted). Alphanumeric short codes (e.g. `https://b23.tv/ZyQrgYf`) are resolved to the real room id by following the 302 redirect (results are cached in-process, no repeated requests) before pulling the stream; a landing page that is not a Bilibili live room, or a link with no room id / an invalid one, produces an explicit error message (links are masked before they reach the logs).

**Huya link formats (mobile share domain and desktop link share one recording pipeline)**: `https://www.huya.com/30764624` | `https://hy.fan/30764624` (mobile share domain, a purely numeric path segment is the room id; trailing slashes, query strings and the `http://` scheme are all accepted). Alphanumeric short codes (e.g. `https://hy.fan/JbmwoV`) are resolved to the real room id by following the 301 redirect (results are cached in-process, no repeated requests) and normalised to the desktop link before pulling the stream; a landing page that is not a Huya live room, or a link with no room id / an invalid one, produces an explicit error message (links are masked before they reach the logs).

> Total of **51** platforms (marketed as 60+, including platforms being added). Platform data-fetching functions are in `src/streams/spider.py`, and stream-URL parsing is in `src/streams/stream.py`.

**Danmaku recording support (5 platforms)**: Douyin Live | Douyu Live | Huya Live | Bilibili Live | TwitchTV

**Actual quality re-sampling and degradation alerts (7 platforms)**: Douyin | TikTok | Kuaishou | Huya | Douyu | Bilibili | NetEase CC

## 📁 Project Structure

Runtime code is grouped by responsibility under `src/`. The single entry is `src/main.py`, which loads the panel in `src/web.py` when `[Web] web_enable` is set; both use the engine in `src/recording/engine.py`.

```text
DouyinLiveRecorder/
├── src/
│   ├── __init__.py                # Explicit Node initialization and danmaku factories
│   ├── main.py / web.py           # Single entry / panel implementation
│   ├── app/                       # Startup cleanup, shutdown and log archiving
│   ├── config/                    # INI parsing, read/write, booleans and backups
│   ├── recording/                 # Engine, sessions, state, scheduling, processes and postprocessing
│   ├── streams/                   # Platform dispatch, live data, stream URLs, quality, selection and signing
│   ├── danmaku/                   # Danmaku capture, subtitles, monitoring, decompression and platforms/ protocols
│   ├── monitors/                  # Account follow and direct-message monitoring
│   ├── notifications/             # Push channels, recording feedback and custom scripts
│   ├── network/                   # HTTP, WebSocket, proxies, credential caches and target validation
│   ├── runtime/                   # FFmpeg/Node installation, resource verification and JS execution
│   ├── api/                       # Web routes, request models and panel configuration
│   ├── support/                   # Logging, localization, console and generic helpers
│   ├── proto/                     # Douyin protocol definitions, generated code and stubs
│   ├── javascript/                # Platform signing assets
│   └── external_resources.json    # External download and integrity metadata
├── web/                       # Web panel static assets
├── i18n/en_US.json            # English translations; Chinese uses source text
├── tests/                     # Python regressions, live collectors and frontend/ checks
├── tool/                      # Build, coverage, translation, smoke and standalone tools
├── typings/                   # Third-party type stubs
├── docs/                      # Architecture references, changelog archives and past reviews
├── .agents/skills/            # Versioned project skills
├── .github/                   # CI, release, image workflows and collaboration templates
├── pyproject.toml             # Version, dependencies and tool settings
├── uv.lock                    # Locked dependencies
├── Dockerfile                 # Multi-stage image build
├── docker-compose.yaml        # CLI / Web services
└── index.html                 # Standalone M3U8 player
```

Runtime directories are created as needed: `config/` holds `config.toml` and `URL_config.ini`; `downloads/`, `logs/` and `backup_config/` hold recordings, logs and backups; `ffmpeg/` and `node/` hold local runtimes. The root `config/` directory contains user data; `src/config/` contains Python code.

See the [file and responsibility map](docs/agent-reference/project-structure.md) for the detailed tree, [architecture guide](CODE_WIKI.md#核心模块详解)（Chinese） for implementation details, and [AGENTS.md](AGENTS.md) for engineering conventions.

## ⚙️ Configuration

### Basic config (config/config.toml)

> Config is TOML: quote section/key names containing CJK characters, parentheses or `|`, and use `true`/`false` for booleans. A legacy `config.ini` is migrated to `config.toml` on first start (the old file is renamed `config.ini.migrated`); values are type-checked on load, invalid values fall back to defaults with a warning, and edits are hot-reloaded on the next detection cycle.

```toml
["录制设置"]
# UI language: zh_CN | en_US (empty = Chinese; missing English catalog falls back to Chinese)
"language" = ""
# Whether to skip proxy detection (yes/no)
"是否跳过代理检测(是/否)" = true
# Whether to enable log files (yes/no)
"是否启用日志文件(是/否)" = true
# Purge stale bytecode caches at startup: runs only when the version or source content changed
# (no-op otherwise). Scope is __pycache__ under the app folder and src/ only - never tests/,
# tool/, downloads/ and similar directories
"是否启动时清理陈旧字节码缓存(是/否)" = true
# Live save path (defaults to downloads/ if empty)
"直播保存路径(不填则默认)" = ""
# When the anchor renames, automatically update URL_config.ini and rename the recording directory/files (default: yes)
"是否自动更新主播名(是/否)" = true
# Whether to separate save folders by author
"保存文件夹是否以作者区分" = true
# Whether to separate save folders by time
"保存文件夹是否以时间区分" = false
# Whether to separate save folders by title
"保存文件夹是否以标题区分" = false
# Whether to include the title in the save file name
"保存文件名是否包含标题" = false
# Whether to strip emoji from names
"是否去除名称中的表情符号" = true
# Video save format ts|mkv|flv|mp4|mp3 audio|m4a audio
"视频保存格式ts|mkv|flv|mp4|mp3音频|m4a音频" = "ts"
# Recording quality 原画|超清|高清|标清|流畅
"原画|超清|高清|标清|流畅" = "原画"
# Custom quality options (comma-separated) — the user-selected quality subset; when empty or all entries are invalid it falls back to the engine's built-in full set (default: empty);
# Web quality option management writes back here; choosing a non-default quality writes "quality,live-room address" into config/URL_config.ini
"自定义画质选项(逗号分隔)" = ""
# Whether to use a proxy IP (yes/no)
"是否使用代理ip(是/否)" = false
# Proxy address
"代理地址" = ""
# Number of threads accessing the network at the same time
"同一时间访问网络的线程数" = 3
# Max concurrent recordings — global concurrency cap, 0 means unlimited (default 0); changes take effect on the next check cycle
"最大同时录制数(0为不限制)" = 0
# Loop interval (seconds) — live-status check interval (default 60)
"循环时间(秒)" = 60
# Queue read URL time (seconds)
"排队读取网址时间(秒)" = 0
# Whether to show the loop countdown
"是否显示循环秒数" = false
# Whether to show the live source URL
"是否显示直播源地址" = false
# Whether segmented recording is enabled
"分段录制是否开启" = true
# Whether HLS capture is enabled (yes/no) — if disabled, only non-HLS candidates such as FLV are used
"是否启用HLS采集(是/否)" = true
# HLS capture exclusion platforms (comma-separated) — listed platforms ignore the "是否启用HLS采集" setting and always use FLV capture
# (platform names must exactly match those shown in logs/config, e.g. 斗鱼直播,虎牙直播; leave empty to exclude nothing)
"HLS采集排除平台(逗号分隔)" = "虎牙直播"
# Whether https recording is enabled — consolidates the former "是否强制启用https录制" and "是否禁用SSL证书验证(是/否)":
# enabled = stream pulled over https and SSL cert verification skipped; disabled = stream pulled over http and default cert verification restored
# (the value of the old key "是否强制启用https录制" is auto-migrated and inherited; https-only overseas platforms like TikTok/YouTube keep their original form when disabled)
"是否启用https录制" = true
# Platforms exempt from SSL cert verification (comma-separated) — only takes effect when "是否启用https录制 = 否" (http mode, cert verification required).
# Since FFmpeg 9.0, TLS cert verification is on by default; platforms with cert anomalies must be exempted here; required platforms are auto-appended at startup
# (Huya Live / Bilibili Live), with only appends and no removal of user-entered items
"禁用SSL证书验证的平台(逗号分隔)" = "虎牙直播,B站直播"
# Recording free-space threshold (gb)
"录制空间剩余阈值(gb)" = 1.0
# Video segment duration (seconds) (default 1800)
"视频分段时间(秒)" = 1800
# Automatically convert to mp4 format after recording completes
"录制完成后自动转为mp4格式" = false
# Re-encode mp4 format to h264
"mp4格式重新编码为h264" = false
# Delete the original file after appending format
"追加格式后删除原文件" = true
# Generate a timestamp subtitle file
"生成时间字幕文件" = false
# Whether to run a custom script after recording completes
"是否录制完成后执行自定义脚本" = false
# Custom script execution command
"自定义脚本执行命令" = ""
# Platforms recorded using a proxy (comma-separated)
"使用代理录制的平台(逗号分隔)" = "tiktok, sooplive, pandalive, winktv, flextv, popkontv, twitch, liveme, showroom, chzzk, shopee, shp, youtu, faceit"
# Additional platforms recorded using a proxy (comma-separated)
"额外使用代理录制的平台(逗号分隔)" = ""
# Whether to record danmaku (yes/no) — when enabled, danmaku is written to SRT subtitle files, synced with video start/stop
"是否录制弹幕(是/否)" = true
# Whether danmaku monitoring is enabled (yes/no) — real-time danmaku viewing only, no SRT written (decoupled from danmaku recording, can be enabled separately)
"是否弹幕监控(是/否)" = true
# Danmaku segment duration (seconds) — SRT segment granularity, recommended to match "视频分段时间(秒)"
"弹幕分片时长(秒)" = 1800
# Danmaku recording platforms (comma-separated) — the 5 currently supported platforms
"弹幕录制平台(逗号分隔)" = "斗鱼直播,B站直播,虎牙直播,抖音直播,TwitchTV"
# Per-recording duration limit in seconds; 0 means unlimited
"单次录制时长上限(秒,0为不限制)" = 0```

### Push config (config/config.toml)

```toml
["推送配置"]
# Optional: 微信|钉钉|tg|邮箱|bark|ntfy|pushplus — multiple values allowed
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

### Cookie config (config/config.toml)

```toml
["Cookie"]
# Required for recording Douyin: fill in a valid cookie copied from the browser at live.douyin.com (must at least include ttwid)
# Leaving it empty will auto-attempt to fetch a visitor ttwid (may trigger risk control; filling it in is recommended)
"抖音cookie" = ""
# Specify Douyin ttwid separately (left empty, it is fetched and process-level cached by src/streams/ttwid.py)
"ttwid" = ""
"快手cookie" = ""
"tiktok_cookie" = ""
"tiktok_guest_cookie" = ""
"虎牙cookie" = ""
"斗鱼cookie" = ""
"yy_cookie" = ""
"b站cookie" = ""
"小红书cookie" = ""
# XHS (Xiaohongshu) app-side session sid carried in the xy-common-params header;
# left empty the built-in default is used (it may already have expired).
# Priority: environment variable XHS_SESSION_SID > this key > built-in default
"xhs_session_sid" = ""
"bigo_cookie" = ""
# ... a total of 51 platform cookie keys; see config.toml for the rest```

> Visitor-type cookies (Douyin ttwid, Kuaishou did, etc.) are process-level shared-cached by `src/network/credentials.py` keyed by "normalized URL + proxy" (default 30-minute TTL), so concurrent rooms do not repeatedly fetch and trigger risk control.

### Authorization config (config/config.toml)

```toml
["Authorization"]
# Token obtained from PopkonTV after login (written back after auto-login with account/password)
"popkontv_token" = ""```

### Account/password config (config/config.toml)

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

### Web management panel config (config/config.toml)

```toml
["Web"]
# Whether the web panel starts with the single entry (false = pure CLI recording, no port)
"web_enable" = false
# Web management panel listen address (localhost-only by default; explicitly change to 0.0.0.0 for Docker/LAN access)
"web_host" = "127.0.0.1"
# Web management panel port
"web_port" = 8000
# Whether password login is enabled (true/false)
"web_auth_enable" = "false"
# Access password (required when auth is enabled)
"web_password" = ""
# Token validity period (seconds)
"web_token_expiry" = 86400
# Whether to show the console window (when false, runs in background; logs written to logs/web_console.log)
"web_show_console" = "true"
# Trusted reverse-proxy sources (comma-separated). Left empty, X-Forwarded-For is not parsed;
# when behind Nginx/Caddy, fill in the proxy IP so login rate-limiting can obtain the real client IP
"web_trusted_proxy" = ""
# Allowed Host / Origin allowlist (comma-separated). Only needed when web_host is bound to
# 0.0.0.0/:: and the panel is reached via a domain name; any unlisted multi-label domain Host is
# rejected to block DNS rebinding (attacker.tld → 127.0.0.1)
"web_allowed_hosts" = ""```

> A full key template is available at [config/config.toml.example](config/config.toml.example); copy it to `config/config.toml` and fill in your values — that file contains credentials and must never be committed.
>
> `web_enable` defaults to `false` (pure CLI recording, no port); Docker and local runs behave the same, driven only by config.toml, and compose injects no override. Port 8000 is always mapped; nothing listens inside the container when the panel is off.
>
> `web_host` is `127.0.0.1` in the repo's default config (localhost access only). For Docker or LAN/public access, change it to `0.0.0.0`, and be sure to also enable `web_auth_enable` and set `web_password`.

Douyin supports the following 5 live-room / profile URL formats (formats for other platforms are shown in the per-platform examples below):

```
# 1) Web anchor live room (numeric room id)
https://live.douyin.com/745964462470

# 2) App anchor live room (share short link)
https://v.douyin.com/iQFeBnt/

# 3) Douyin-id concatenation (https://live.douyin.com/ + Douyin id, supports VR live recording)
https://live.douyin.com/yall1102

# 4) App anchor profile (share short link)
https://v.douyin.com/CeiU5cbX

# 5) Web anchor profile (user page address)
https://www.douyin.com/user/MS4wLjABAAAA3kr2yA4aRD-sjf9cx8xkOH8Di3RjktpKcAvqIetpsF0
```

> Notes:
> - Formats 1/3/5 use the web endpoint (support VR live); formats 2/4 use the app endpoint
> - Format 5 (web anchor profile `www.douyin.com/user/<sec_uid>`) directly extracts `sec_user_id` from the address and resolves the Douyin id, then records via the web endpoint as a live-room address — no app-endpoint probing needed
> - Short-link forms such as format 4 (app profile) first probe the live-room address; on failure they automatically fall back to Douyin-id resolution and then record via the web endpoint

```ini
# Specify quality (quality, live-room address)
超清，https://live.douyin.com/745964462470

# Specify quality and anchor name (quality, live-room address, anchor: name)
高清，https://live.bilibili.com/123456，主播: B站主播

# Comment out a live room (prefix the address with #)
# https://live.douyin.com/123456789```

### Environment variable config

| Variable | Description | Example |
| --- | --- | --- |
| `PYTHONUNBUFFERED` | Output logs in real time | `1` |
| `PYTHONDONTWRITEBYTECODE` | Do not generate .pyc files | `1` |
| `PYTHONIOENCODING` | Python output encoding | `utf-8` |
| `TZ` | Timezone setting | `Asia/Shanghai` |
| `TERM` | Terminal type | `xterm-256color` |

## 🎬 Usage

### Start (single entry)

```bash
python -m src.main
```

Whether the web management panel starts alongside is controlled by `[Web] web_enable` in `config/config.toml` (default `false`, i.e. pure CLI recording). When set to `true`, the same command also listens on the panel port; open `http://localhost:8000` in a browser. `python -m src.web` is no longer a startup path (`src/web.py` is only the panel implementation module).

Features:
- **Dashboard**: real-time view of monitored/recording counts, error count, remaining disk, recording list, and log stream (SSE push)
- **Room management**: online CRUD of live-room addresses, enable/disable (auto hot-loaded into the recording main loop)
- **Config editor**: edit each config.toml item online (recording settings/push/Cookie, etc.); sensitive config items are masked
- **File browser**: browse the downloads directory and download recording files
- **Actual quality display**: the recording table shows "set quality / actual quality", highlighted in red on degradation
- **Danmaku viewer**: reads the danmaku monitor hub snapshot and shows each room's danmaku events in real time
- **Language switch**: top-bar language selector, instant Chinese/English switching (writes back config and hot-switches in-process translations, no restart needed)

Main API routes (all require a Token, except when auth is disabled):

| Route | Method | Function |
| --- | --- | --- |
| `/api/login` | POST | Password login, returns Token |
| `/api/status` | GET | Recording status (incl. actual quality) |
| `/api/status/stream` | GET | Recording-status SSE stream |
| `/api/rooms` | GET/POST/PUT/DELETE | Live-room CRUD |
| `/api/rooms/toggle` | POST | Enable / disable a live room |
| `/api/config` | GET/PUT | Read / modify config |
| `/api/language` | GET/PUT | Query / switch UI language |
| `/api/files`, `/api/files/download` | GET | Recording file browse and download |
| `/api/logs`, `/api/logs/stream` | GET | Log query / SSE real-time push |
| `/api/danmaku` | GET | Danmaku monitor snapshot |

The Web mode shares the same recording engine and config file with CLI mode; CRUD of live-room addresses is auto hot-loaded by the recording main loop.

Background mode: set `web_show_console` to `false`; under Windows the console window is hidden, logs are written to `logs/web_console.log`, and the program runs fully in the background.


> ⚠️ **Security note**: the default bind is localhost-only (`127.0.0.1`) with authentication disabled. If Docker, LAN, or public access requires binding to `0.0.0.0`, enable `web_auth_enable` and set a strong password at the same time.

### Recommended recording formats

- **Long recordings**: `ts` is recommended — written in real time, resilient to corruption on power loss
- **Short recordings**: `mp4` or `mkv` is recommended — directly usable after recording completes
- **Audio-only recording**: `mp3` or `m4a` is recommended

### Quality notes

| Quality code | Chinese name | Description |
| --- | --- | --- |
| OD | 原画 | Original Definition, highest quality |
| BD | 蓝光 | Blu-ray, ultra-high definition |
| UHD | 超清 | Ultra HD |
| HD | 高清 | High Definition |
| SD | 标清 | Standard Definition |
| LD | 流畅 | Low Definition, lowest quality |

Supported platforms: Douyin, TikTok, Kuaishou, Huya, Douyu, Bilibili, NetEase CC. When a platform's actually delivered quality is lower than the configured quality, an automatic alert and flag are raised.

### Source selection (HLS/FLV)

The recording stream source is automatically selected between HLS (m3u8, pulled segment by segment) and FLV (a single long connection): by default HLS is preferred, falling back to FLV in order when validation fails, with `record_url` as the final fallback. It is controlled by two config items together:

| Config item | Effect |
| --- | --- |
| `是否启用HLS采集(是/否)` | Global switch: when enabled (default), the HLS source is preferred and falls back to FLV when unreachable or absent; when disabled, all platforms use only non-HLS candidates such as FLV |
| `HLS采集排除平台(逗号分隔)` | Platform-level exclusion list: listed platforms **ignore the global switch and always use FLV capture** (equivalent to disabling HLS capture for that platform only — HLS sources take no part in candidates or fallback) |

- **Use case**: you prefer HLS recording overall (HLS pulls segment by segment and is immune to a single long connection being cut off by the CDN), but an individual platform's HLS source is unstable or risk-controlled, and you want only that platform forced onto FLV — just add it to the exclusion list, without having to turn off the global HLS switch
- **Fill format**: platform names must **exactly match** what the logs/config file show (e.g. `斗鱼直播`, not `斗鱼`); separate multiple platforms with commas (Chinese or English commas both work), e.g. `斗鱼直播,虎牙直播`; leave empty (default) to exclude nothing — behavior stays identical to previous versions
- **Priority semantics**: the exclusion list takes precedence over the global switch — even with "是否启用HLS采集(是/否) = 是" (yes), a listed platform only uses FLV; platforms outside the list are completely unaffected and keep HLS priority per the global config
- **Boundary behavior**: if an excluded platform's parsed result contains only an HLS source with no FLV/record_url fallback, the round is abandoned with a warning (the log suggests "remove the platform from the exclusion list to restore HLS capture"); an h265-encoded FLV source no longer triggers a switch to HLS (the logic that auto-saves h265 FLV recordings as TS format is unaffected)
- **Hot reload**: the main loop re-reads the config every round — save your changes and the next monitoring round picks them up, no restart needed

### Danmaku recording and danmaku monitoring

The danmaku features are controlled by two **mutually decoupled** toggles, which can be enabled separately or together:

| Config item | Effect |
| --- | --- |
| `是否录制弹幕(是/否)` | Danmaku is written to SRT subtitle files, synced with video start/stop |
| `是否弹幕监控(是/否)` | Real-time danmaku viewing only, no SRT written (Web `/api/danmaku`) |
| `弹幕分片时长(秒)` | SRT segment granularity, recommended to match "视频分段时间(秒)" |
| `弹幕录制平台(逗号分隔)` | Whitelist of platforms with danmaku enabled |

- **Supported platforms (5)**: Douyin Live, Douyu Live, Huya Live, Bilibili Live, TwitchTV
- **Output files**: SRT corresponds one-to-one with video segments, named `{base name}_{segment index:03d}.srt` (e.g. `_000.srt` corresponds to `_000.ts`); when not segmented, `{base name}.srt`. The timeline is based on a monotonic clock, aligned with ffmpeg's `segment -reset_timestamps` PTS, and can be loaded directly by players
- **Danmaku direct connect**: the danmaku WebSocket explicitly does not follow the system proxy, avoiding immediate disconnection under a SOCKS proxy
- **Monitor sidecar log**: danmaku monitor events are also written to `logs/danmaku_monitor.jsonl` (5MB rotation)
- Douyin danmaku auto-fetches a visitor ttwid when the cookie is empty; Bilibili danmaku auto-fetches a real buvid (login cookie → spi endpoint → homepage Set-Cookie → random fallback)

### Auto anchor-name update

With `是否自动更新主播名(是/否)` enabled (default "yes"), whenever the latest anchor name is resolved each round and differs from the name recorded in `URL_config.ini`, the program automatically does two things:

1. **Rename the filesystem**: `{save path}/{platform}/{old anchor name}` → new anchor-name directory; recursively rename all recording artifacts (TS / FLV / SRT / subtitles) in the directory tree that start with `{old name}_`, and title directories ending with `_{old name}`; if the new-name directory already exists, items are merged and moved in one by one (compatible with an anchor reverting to a former name)
2. **Write back the config file**: precisely match by URL segment and replace only that line's anchor-name field, fully preserving the quality segment, the `#` comment prefix, and the line-ending style; the operation is idempotent

Safety constraints:

- The detection point is located "after live data is parsed, before recording starts", at which time the thread is necessarily not recording — naturally avoiding the ffmpeg file-occupancy window
- **Filesystem first, then config file; the name used this round is switched only after both succeed**; on any failure, the old name is kept and retried automatically on the next poll round
- When an individual file is occupied by background transcoding/a player, only a warning is skipped past — it does not block the whole process
- Custom stream addresses (whose anchor name contains a per-round random UUID) and nicknames that become blank after cleaning are automatically skipped
- Disabling this toggle keeps manual names unchanged

### Multi-language and UI switching

Chinese is the default source language; English loads `i18n/en_US.json`:

| Language code | Display name | Catalog file |
| --- | --- | --- |
| `zh_CN` | Chinese | Source text; no catalog needed |
| `en_US` | English (US) | `i18n/en_US.json` |

- The config key `language`: empty means Chinese. English region codes normalize to `en_US`, Chinese region codes to `zh_CN`; invalid values or unavailable English translations fall back to Chinese.
- **Hot switching**: switchable via the Web panel top-bar language selector or directly editing `config.toml`; the CLI main loop checks for config changes each round and reloads translations immediately, **no process restart needed** (the running ffmpeg subprocess is unaffected)
- Translations no longer depend on the `LANG` / `LANGUAGE` environment variables (generally unset on Windows)
- Maintain only `i18n/en_US.json`. Chinese displays source text directly; no compilation or separate validation script is needed.

### Stop recording

- **Windows**: press `Ctrl+C` in the terminal
- **Linux/macOS**: press `Ctrl+C` in the terminal
- **Docker**: run `docker-compose stop`

### Notes

1. To record overseas platforms such as TikTok or SOOP (formerly AfreecaTV), enable the proxy in the config
2. For long uptime, set a longer loop interval (e.g. 60 seconds) to avoid frequent requests getting your IP banned
3. Files are saved automatically after the live stream ends — no manual stop needed
4. If a recorded video file is corrupted, `ts` format recording is recommended
5. Recording Douyin requires a valid cookie (at least containing ttwid), otherwise risk control may be triggered
6. Some platforms need a Node.js environment to run JavaScript signature scripts, which is auto-installed under Windows

## 🐋 Docker Deployment

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) installed
- [Docker Compose](https://docs.docker.com/compose/install/) installed

### Quick start

```bash
# 1. Clone the project
git clone https://github.com/y123ao6/DouyinLiveRecorder.git
cd DouyinLiveRecorder

# 2. Edit the config file
# Add live-room addresses in config/URL_config.ini

# 3. Start the container (default CLI recording mode)
docker compose up -d

# 4. View logs
docker compose logs -f
```

### Pull the prebuilt multi-arch image (GHCR)

The `.github/workflows/docker-publish.yml` workflow builds and pushes one layer per native runner (amd64 and arm64)
on every `v*` tag and then merges them by manifest digest — so **a single `docker pull` resolves to the layer matching
the host architecture**:

```bash
# Replace <owner> with the GitHub account that owns the image (GHCR namespaces are always lower-cased)
docker pull ghcr.io/<owner>/douyin-live-recorder:latest
docker run -d -v ./config:/app/config -v ./downloads:/app/downloads \
  -v ./logs:/app/logs -v ./backup_config:/app/backup_config \
  ghcr.io/<owner>/douyin-live-recorder:latest
```

- Tags: `latest` and `vX.Y.Z`; `latest` is produced only from a `v*` tag and cannot be overwritten by a manual run.
  The two per-architecture tags (`:vX.Y.Z-amd64` / `-arm64`) are intermediate products of the merge step and are not
  cleaned up automatically at the moment.
- Anonymous pulls require the GHCR package visibility to be **public** — a one-off manual action in the repository settings.
- This is **deliberately a different name** from `image: y123ao6/douyin-live-recorder:latest` in `docker-compose.yaml`:
  compose goes through `pull_policy: build` and builds in place (it does not pull a same-named registry image), so the
  "Quick start" instructions above stay as they are. Use the `docker run` line above for the GHCR image; on an arm64
  host the in-place compose build works just as well.

### Run mode

`docker-compose.yaml` has a single service with the single entry `python -m src.main`; whether the panel starts is controlled by `[Web] web_enable` in `config/config.toml`:

```bash
# Plain recording (default; web_enable = false)
docker compose up -d

# Panel + recording: set web_enable = true, start with the same command, open http://localhost:8000
```

> Docker supports CLI / Web only.

> ⚠️ Web-mode note: The panel listens on `127.0.0.1` by default; inside the container you must set `web_host = 0.0.0.0` in the `[Web]` section of `config/config.toml` to access it from the host; it is also recommended to enable `web_auth_enable = true` and set `web_password`.

### Data mounts

```yaml
volumes:
  - ./config:/app/config:rw          # config directory (required)
  - ./downloads:/app/downloads:rw    # recording download directory (required)
  - ./logs:/app/logs:rw              # runtime log directory
  - ./backup_config:/app/backup_config:rw  # config backup directory
```

### Port mapping

The single service always maps the panel port; nothing listens inside the container when `web_enable = false` (connection refused, recording unaffected):

```yaml
ports:
  - "127.0.0.1:8000:8000"   # Panel port (actually listening only when web_enable = true; local host only by default)
```

### Environment variables

> Same table as "Environment variable config" above (`TZ` / `PYTHONUNBUFFERED` / `PYTHONDONTWRITEBYTECODE` / `PYTHONIOENCODING` / `TERM`); compose injects from that table, so it is not repeated here.

### Docker image features

- **Multi-stage build**: the builder stage installs dependencies into a virtualenv; the runtime stage is a slim image
- **Non-root user**: runs as the `recorder` user for improved security
- **Health check**: automatically detects whether the `python -m src.main` process is alive (same process name in panel mode)
- **Resource limits**: defaults to 2 CPU / 2G memory (adjustable in docker-compose.yaml)
- **Log rotation**: single file 50MB, up to 3 retained
- **Built-in Node.js 24 LTS**: for running JavaScript signature scripts

## ❤️ Contributors

<a href="https://github.com/y123ao6/DouyinLiveRecorder/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=y123ao6/DouyinLiveRecorder" />
</a>

## 📄 License

This project is open-sourced under the [MIT License](LICENSE). Stars and Forks are welcome!

## 💬 For questions or requests, please open an Issue. Stars and Forks are welcome

[![Star History Chart](https://api.star-history.com/svg?repos=y123ao6/DouyinLiveRecorder&type=Timeline)](https://star-history.com/#y123ao6/DouyinLiveRecorder&Timeline)
