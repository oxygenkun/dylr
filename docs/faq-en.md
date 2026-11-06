# FAQ

[Back to README](../README_EN.md) · [简体中文](faq.md)

**Q: Recording shows "ffmpeg missing, cannot record"**

```bash
# Ubuntu/Debian
sudo apt install ffmpeg

# macOS
brew install ffmpeg

# Windows
# The program ships with ffmpeg, no install needed
```

**Q: Which ffmpeg is actually used on an Apple Silicon (M-series) Mac?**

The macOS ffmpeg bundled in the full release package is upstream's **x86_64 static build**
(ffmpeg.org only publishes "Static builds for macOS 64-bit" and **no arm64 build**), so on
Apple Silicon it runs through **Rosetta 2** translation. To keep it from shadowing a native
build the user installed themselves: when "macOS + arm64 + another ffmpeg already exists on the
system PATH" all hold, the program **prefers the system one** (e.g. what `brew install ffmpeg`
installs, a native arm64 build) and no longer prepends the bundled `ffmpeg/` directory to `PATH`.
In every other case (Intel Mac, Windows, Linux, no ffmpeg on the system, or the PATH hit turns
out to be the bundled copy itself) the previous behaviour is kept and the bundled build is used.
The single-file script `douyin_live_recorder_standalone.py` applies the same criteria; there it
shows up as `find_ffmpeg()` returning the system build's path instead of reordering `PATH`.

How to confirm which copy is in effect:

```bash
ffmpeg -version          # a Homebrew build carries --prefix=/opt/homebrew/... (native on Apple Silicon)
                          # the bundled evermeet static build has no such prefix
which ffmpeg              # points into the bundled ffmpeg/ directory => the bundled copy is in use
```

Or look for the "FFmpeg PATH priority" line in the debug log (written to `logs/streamget.log`
when `config.toml` has `是否启用日志文件 = 是`), which states explicitly whether the program is
"yielding to the native system ffmpeg <path>" or "still prepending the bundled directory <path>".

> Docker is unaffected by this policy: the image already provides ffmpeg, and on Apple Silicon
> the Linux arm64 container is built/run for the host architecture, so it is native already.

**Q: Shows "Node.js missing" or "execjs"-related errors**

```bash
# Ubuntu/Debian
curl -fsSL https://deb.nodesource.com/setup_24.x | bash -
sudo apt-get install -y nodejs

# macOS
brew install node

# Windows
# The program auto-downloads and installs into the node/ directory
```

**Q: Shows "IP banned, please change device or network"**

- Check whether the proxy is enabled
- Lower the loop monitoring frequency
- Wait a while and try again

**Q: Douyin risk control prevents data retrieval**

- Fill `config.toml`'s `[Cookie]` section with a valid cookie copied from the browser at `live.douyin.com` (must at least include `ttwid`)
- Lower the loop monitoring frequency (the default 120-second loop is already conservative; you may increase it as appropriate)
- Change IP or use a proxy
- Key troubleshooting points (verified conclusions):
  - The typical signal of Douyin risk control is **HTTP 200 + empty response body**, not a 4xx error code; seeing `web/enter` return `status_code=10002 / unknown error` then auto-falling back to HTML scraping is a **normal fault-tolerant path**, not a recording failure
  - Douyin API requests must use a **desktop User-Agent**; the old mobile UA is silently throttled (returns empty body)
  - For profile-type links (formats 4/5), fill in the complete address directly; the old `iesdouyin.com/share/user/` path has become an anti-scraping shell page — do not use it

**Q: HLS check log is blank and always falls back to FLV**

- Symptom: the log shows `get_response_status check failed (judged unreachable): ` (blank message) immediately followed by `HLS URL validation failed, falling back to FLV`, repeating
- Cause (fixed on 2026-08-05):
  - Under Windows, `socket.timeout` / `TimeoutError`'s `str()` is empty, causing exception logs to show blank — impossible to tell whether it was a timeout, connection refused, or a cert issue
  - The stream-URL check function used to silently swallow all exceptions (`except Exception: return False`), so when falling back to FLV there was no cause to inspect
  - The m3u8 source HEAD probe did not cover 404 (Douyin and other CDNs often return 404 for HEAD while GET pulls fine), and from HLS source selection to the check call the **proxy was not forwarded**, causing TikTok and other proxy-required platforms to time out on direct-connect checks and be misjudged unreachable
- Behavior after the fix: exception logs include the URL and exception type; all failure paths log detailed warnings (with status code / content-type); m3u8 HEAD non-2xx (**including 404**) always gets a `Range: bytes=0-0` GET probe; HLS source selection correctly forwards the proxy. If it still falls back after re-running, the log will directly give the real cause (e.g. `ConnectTimeout`, `HEAD=404, Range-GET=403`) — at that point it is usually an environmental issue such as the CDN domain being blocked or the anchor's stream URL expiring, not a code misjudgment

**Q: Recorded video file is corrupted**

- `ts` format recording is recommended
- Check that disk space is sufficient
- Check that the network is stable

**Q: How to push live-start notifications only, without recording?**

Set `只推送通知不录制(是/否) = 是` in the `[推送配置]` section of `config.toml`

**Q: Forgot the Web panel password?**

Directly edit the `web_password` item in `config/config.toml`; after changing it, restart the service (`python -m src.main`). After a password change, all existing Tokens are invalidated and require re-login.
