# 常见问题

[返回 README](../README.md) · [English](faq-en.md)

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

**Q: Apple Silicon（M 系列芯片）Mac 上实际用的是哪一份 ffmpeg？**

发布包（full 版）内置的 macOS ffmpeg 是上游的 **x86_64 静态构建**（ffmpeg.org 官方只发布
「Static builds for macOS 64-bit」，**不提供 arm64 构建**），在 Apple Silicon 上经 **Rosetta 2**
转译执行。为避免它遮蔽用户自己安装的原生构建：当「macOS + arm64 + 系统 PATH 上已有另一份
ffmpeg」三条同时成立时，程序**优先使用系统那份**（例如 `brew install ffmpeg` 装上的原生
arm64 构建），不再把包内 `ffmpeg/` 目录前置到 `PATH`；其余情况（Intel Mac、Windows、Linux、
系统里没有 ffmpeg、或 PATH 上探到的其实就是包内那一份）维持原样，仍使用包内那份。
单文件版脚本 `douyin_live_recorder_standalone.py` 采用同一判据，只是表现为「`find_ffmpeg()` 直接
返回系统那份的路径」而不是调整 `PATH`。

确认当前生效的是哪一份：

```bash
ffmpeg -version          # Homebrew 安装的会带 --prefix=/opt/homebrew/...（Apple Silicon 原生）
                          # 包内的 evermeet 静态构建没有该前缀
which ffmpeg              # 指向包内 ffmpeg/ 目录 = 用的是包内那份
```

或查看 debug 日志（`config.toml` 中 `是否启用日志文件 = 是` 时写入 `logs/streamget.log`）里的
「FFmpeg PATH 优先级」一行，它会明确写出「让位于系统原生 ffmpeg <路径>」还是「仍前置包内目录 <路径>」。

> Docker 镜像不受此策略影响：镜像内的 ffmpeg 由 apt 安装，Apple Silicon 上按本机架构构建/运行
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
- 降低循环监测频率（默认循环时间 120 秒已较保守，可酌情调大）
- 更换 IP 或使用代理
- 排查要点（实测结论）：
  - 抖音风控的典型信号是 **HTTP 200 + 空响应体**，而非 4xx 错误码；日志里看到 `web/enter` 返回 `status_code=10002 / unknown error` 后自动回退 HTML 抓取是**正常的容错链路**，不代表录制失败
  - 请求抖音接口必须使用**桌面端 User-Agent**，旧版移动端 UA 会被静默限流（返回空 body）
  - 主页类链接（格式 4/5）请直接填写完整地址；`iesdouyin.com/share/user/` 旧路径已变为反爬壳页，请勿使用

**Q: HLS 校验失败日志空白，总是回退到 FLV**

- 现象：日志出现 `get_response_status 校验失败（判定为不可达）: `（消息空白）后紧跟 `HLS URL validation failed, falling back to FLV`，且反复出现
- 原因（已修复于 2026-08-05）：
  - Windows 下 `socket.timeout` / `TimeoutError` 的 `str()` 为空，导致异常日志显示为空白，无法判断是超时、连接被拒还是证书问题
  - 流地址校验函数原先静默吞掉所有异常（`except Exception: return False`），回退 FLV 时无任何原因可查
  - m3u8 源 HEAD 探测原先未覆盖 404（抖音等 CDN 常对 HEAD 返回 404 而 GET 可正常拉流），且从 HLS 源选择到校验调用**未透传代理**，导致 TikTok 等需代理平台直连校验超时误判不可达
- 修复后表现：异常日志会带 URL 与异常类型；所有失败路径记录详细警告（含状态码 / content-type）；m3u8 HEAD 非 2xx（**含 404**）一律补 `Range: bytes=0-0` GET 探测；HLS 源选择正确透传代理。重新运行后若仍回退，日志会直接给出真实原因（如 `ConnectTimeout`、`HEAD=404, Range-GET=403`），此时多为 CDN 域名被墙或主播流地址失效等环境问题，而非代码误判

**Q: 录制的视频文件损坏**

- 推荐使用 `ts` 格式录制
- 检查磁盘空间是否充足
- 检查网络是否稳定

**Q: 如何只推送开播通知不录制？**

在 `config.toml` 的 `[推送配置]` 节设置 `只推送通知不录制(是/否) = 是`

**Q: Web 面板忘记密码怎么办？**

直接编辑 `config/config.toml` 中的 `web_password` 项，修改后重启服务（`python -m src.main`）即可。密码变更后所有现有 Token 会自动失效，需重新登录。
