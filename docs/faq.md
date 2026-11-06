# 常见问题

[返回 README](../README.md)

**Q: 录制时提示 "缺少 ffmpeg 无法进行录制"**

```bash
# Ubuntu/Debian
sudo apt install ffmpeg

# macOS
brew install ffmpeg

# Windows：full 版运行包已自带 ffmpeg，无需安装；
# lite 版 / 单文件版若自动安装失败，按 README「快速开始 → 手动安装 ffmpeg（Windows 用户指南）」放置
# 为 <程序目录>\ffmpeg\ffmpeg.exe即可，不要留在 ffmpeg\bin\ 子目录下。
```

Windows 自动下载使用 `src/external_resources.json` 中的地址与校验值。下载或校验失败时可手动安装：

1. 下载官方 zip：<https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip>；需要核对来源时比对同目录的 `.zip.sha256`（程序自动安装校验用的就是这份摘要）。
2. 把解压出的 `bin` 文件夹放进程序目录并改名为 `ffmpeg`：

| 运行方式 | 放置结果 |
| --- | --- |
| 下载的运行包（exe） | `dylr\ffmpeg\ffmpeg.exe` |
| 源码运行 / 单文件版脚本 | `<项目根>\ffmpeg\ffmpeg.exe` |

不要保留成 `ffmpeg\bin\ffmpeg.exe`——程序只把 `ffmpeg\` 这一层加入查找路径，不会进子目录寻找。

3. 在 `ffmpeg\` 目录执行 `ffmpeg -version`，能打印版本号即成功；重启程序后 `logs\streamget.log` 不再出现「未安装 ffmpeg。」即生效。

也可改用系统级安装（`winget install Gyan.FFmpeg`、Chocolatey 或可信构建）放入 `PATH`，程序同样按 `PATH` 查找。full 包已内置运行时；macOS 归档使用 GPG 验签，缺少 gpg 时请手动安装；Docker 使用镜像内组件，不自动下载。

**Q: Apple Silicon（M 系列芯片）Mac 上实际用的是哪一份 ffmpeg？**

发布包（full 版）内置的 macOS ffmpeg 是上游的 **x86_64 静态构建**（ffmpeg.org 官方只发布
「Static builds for macOS 64-bit」，**不提供 arm64 构建**），在 Apple Silicon 上经 **Rosetta 2**
转译执行。为避免它遮蔽用户自己安装的原生构建：当「macOS + arm64 + 系统 PATH 上已有另一份
ffmpeg」三条同时成立时，程序**优先使用系统那份**（例如 `brew install ffmpeg` 装上的原生
arm64 构建），不再把包内 `ffmpeg/` 目录前置到 `PATH`；其余情况（Intel Mac、Windows、Linux、
系统里没有 ffmpeg、或 PATH 上探到的其实就是包内那一份）维持原样，仍使用包内那份。
单文件版脚本 `dylr_standalone.py` 采用同一判据，只是表现为「`find_ffmpeg()` 直接
返回系统那份的路径」而不是调整 `PATH`。

确认当前生效的是哪一份：

```bash
ffmpeg -version          # Homebrew 安装的会带 --prefix=/opt/homebrew/...（Apple Silicon 原生）
                          # 包内的 evermeet 静态构建没有该前缀
which ffmpeg              # 指向包内 ffmpeg/ 目录 = 用的是包内那份
```

或查看 debug 日志（`config.toml` 中 `是否启用日志文件 = 是` 时写入 `logs/streamget.log`）里的
「FFmpeg PATH 优先级」一行，它会明确写出「让位于系统原生 ffmpeg <路径>」还是「仍前置包内目录 <路径>」。

> Docker 镜像不受此策略影响：镜像已内置 ffmpeg，Apple Silicon 上按本机架构构建/运行
> Linux arm64 容器，本来就是原生执行。

**Q: 提示 "缺少 Node.js" 或 "execjs" 相关错误**

```bash
# Ubuntu/Debian
curl -fsSL https://deb.nodesource.com/setup_24.x | bash -
sudo apt-get install -y nodejs

# macOS
brew install node

# Windows
# 程序会自动下载安装到 node/ 目录
```

**Q: 提示 "IP 被禁止，请更换设备或网络"**

- 检查是否开启了代理
- 降低循环监测频率
- 等待一段时间后再尝试

**Q: 抖音风控无法获取数据**

- 在 `config.toml` 的 `[Cookie]` 节填入从浏览器 `live.douyin.com` 复制的有效 cookie（至少包含 `ttwid`）
- 降低循环监测频率（默认循环时间 60 秒，可酌情调大）
- 更换 IP 或使用代理
- 排查要点（实测结论）：
  - 抖音风控的典型信号是 **HTTP 200 + 空响应体**，而非 4xx 错误码；排查时先看响应体长度，但空响应也需结合网络与日志判断，不能单凭它确定 UA/Cookie 被拒
  - `web/enter` 返回 `status_code=10002 / unknown error` 可为瞬时软拒绝（风控、缺少 msToken 或限流）。当前实现先静默重试 1 次，再回退 HTML 抓取；若兜底成功，属于**正常的容错链路**，不代表房间不可用或录制失败
  - 请求抖音接口应使用**桌面端 User-Agent**；旧版移动端 UA 曾被静默限流（返回空 body）。账号解析使用 [room.py](../src/streams/room.py) 的 `DESKTOP_UA`，不要直接套用录制流探针的 UA
  - 主页类链接（格式 4/5）请直接填写完整地址；`iesdouyin.com/share/user/` 曾返回反爬壳页，不应依赖 HTML 正则提取 `unique_id`。当前账号解析优先走接口，旧页面仅为兜底

**Q: HLS 校验失败日志空白，总是回退到 FLV**

- 历史现象：日志出现 `is_reachable 校验失败（判定为不可达）: `（消息空白）后紧跟 `HLS URL validation failed, falling back to FLV`，且反复出现
- 原因（已修复于 2026-08-05）：
  - Windows 下 `socket.timeout` / `TimeoutError` 的 `str()` 为空，导致异常日志显示为空白，无法判断是超时、连接被拒还是证书问题
  - 流地址校验函数原先静默吞掉所有异常（`except Exception: return False`），回退 FLV 时无任何原因可查
  - m3u8 源 HEAD 探测原先未覆盖 404（抖音等 CDN 常对 HEAD 返回 404 而 GET 可正常拉流），且从 HLS 源选择到校验调用**未透传代理**，导致 TikTok 等需代理平台直连校验超时误判不可达
- 当前表现：异常日志带脱敏 URL 与异常类型；流地址校验记录状态码 / content-type，通用 `is_reachable` 的部分诊断在 DEBUG 级别。HEAD 未确认可用的 m3u8（包括常见的 404）会补 `Range: bytes=0-0` GET 探测；HLS 源选择透传代理，并继续检查媒体分片。现行实现见 [selection.py](../src/streams/selection.py) 与 [http.py](../src/network/http.py)
- 若仍回退，按 `ConnectTimeout`、`HEAD=404, Range-GET=403` 等具体日志检查代理、CDN 可达性与流地址是否过期；不要仅凭 HEAD 失败判断直播流失效。部分平台无备选源时会交由 ffmpeg 最后尝试，因为探针与 ffmpeg 的客户端指纹不同

**Q: 弹幕连接失败，提示 `connecting through a SOCKS proxy requires python-socks`**

- 历史根因是 WebSocket 库自动跟随系统 SOCKS 代理；视频拉流与弹幕使用不同通道，视频正常并不代表弹幕连接正常。macOS 的 `urllib.request.getproxies()` 还会读取系统网络代理设置，因此 shell 中没有代理环境变量也不能排除系统代理；可用 `scutil --proxy` 检查
- 当前 [WebSocketClient](../src/network/websocket.py) 默认 `proxy=None`，不自动跟随系统代理或 `ALL_PROXY`；需要代理的平台可显式传入代理，不再是所有平台强制直连。[Twitch](../src/danmaku/platforms/twitch.py) 未收到配置代理时还会读取系统 HTTP/HTTPS 代理并显式传入。先更新到当前版本，再检查对应平台实际传入的代理，不要为解决意外代理而盲目增加 `python-socks` 或修改全局系统设置
- 查看 [streamget.log](../logs/streamget.log) 的 DEBUG 记录，按「采集线程启动 → 连接就绪 → 收到首条弹幕 → 连接关闭原因」定位。分享日志前脱敏 Cookie、Token 与带签名 URL
- B站能连接但无弹幕/几十秒后断开，还应区分两个已修复的历史问题：[get_bilibili_danmaku_info](../src/streams/spider.py) 先用 `room/v1/Room/room_init` 将短号转换为真实房间号再取弹幕鉴权；[心跳循环](../src/network/websocket.py) 会检查并等待异步心跳回调，避免协程未执行导致服务端断连

**Q: 抖音、斗鱼显示「正在直播中」，却无报错、无录制文件**

- 历史缺陷是 `get_record_headers()` 返回 `None` 时，`if headers:` 错误包住整个录制链，导致没有专属 Referer/Origin 且未配置 Cookie 的平台静默跳过录制；虎牙、B站有专属头，因而可能正常。不要把「补 Cookie 后恢复」误判为必须配置 Cookie 才能录制
- 当前唯一入口为 `python -m src.main`；Web 面板由 `[Web] web_enable` 控制，[web.py](../src/web.py) 不提供独立启动入口。录制实现已移至 [session.py](../src/recording/session.py)，其中 `_build_ffmpeg_input_args()` 只按需插入 `-headers`，TLS、代理与后续录制不依赖 headers 是否为空
- 排查先确认不是仅通知模式、录制并发槽等待或选源失败，再沿 `select_source_url` 返回值与「准备开始录制视频」日志定位。确需临时插桩时只记录脱敏后的流地址，并核对条件块边界；「条件为假而整块跳过」可能既无异常也无日志
- 流地址探针与 ffmpeg 必须使用一致的 GET 请求语义、Referer/Cookie 和 UA；[selection.py](../src/streams/selection.py) 与录制链共用 `get_record_user_agent()` / `MOBILE_UA`，避免斗鱼等 CDN 因 UA 不同出现探针 403。不要将账号接口所需的桌面 UA 一概用于所有录制流

**Q: 录制的视频文件损坏**

- 推荐使用 `ts` 格式录制
- 检查磁盘空间是否充足
- 检查网络是否稳定

**Q: 如何只推送开播通知不录制？**

在 `config.toml` 的 `[推送配置]` 节设置 `只推送通知不录制(是/否) = 是`

**Q: Web 面板忘记密码怎么办？**

直接编辑 `config/config.toml` 中的 `web_password` 项，修改后重启服务（`python -m src.main`）即可。密码变更后所有现有 Token 会自动失效，需重新登录。

> 返回[使用说明](usage.md) / [文档导航](README.md)；开发侧排障见[已知坑](agent-reference/known-pitfalls.md)。
