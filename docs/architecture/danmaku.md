# 弹幕与字幕

[架构导航](README.md) · [文档导航](../README.md)

## 弹幕采集子系统 (`src/danmaku/platforms/` + `src/danmaku/collector.py` + 关联模块)

**职责与架构总览**: `src/danmaku/` 管理弹幕协议、采集线程、实时监控和 SRT 分片。WebSocket 传输与凭据缓存分别复用 `src/network/websocket.py`、`src/network/credentials.py`；抖音协议定义在 `src/proto/`。弹幕可随视频录制，也可仅监控而不写字幕。

**与流解析解耦**: 弹幕子系统与 `src/streams/spider.py`（视频流地址解析）是**平行的两套抽象**。`spider.py` 负责解析视频流地址，弹幕客户端经 `src/__init__.py` 的注册表/工厂解耦；`spider.py` 不 import `src/danmaku/platforms/`。仅 B站弹幕在 AUTH 被拒时会懒加载回调 `spider.invalidate_bili_buvid_cache()`。

**生命周期接线**（与录制同起同停）:

- `streams/dispatch.py` 将平台弹幕参数写入房间上下文。
- `recording/session.py` 将参数传入 `check_subprocess()`，经 `src/__init__.py` 的 `get_danmaku_collector()` 工厂创建采集器；平台不支持或参数为空时返回 `None`。
- `DanmakuCollector` 共用启动与停止流程，`stop()` 支持重复调用。

**平台注册表**（`src/__init__.py:get_danmaku_class`，平台名与 `src/streams/dispatch.py` 标识一致）:

| 平台标识 | 弹幕类（`src/danmaku/platforms/`） |
| --- | --- |
| 斗鱼直播 | `DouyuDanmaku` |
| B站直播 | `BilibiliDanmaku` |
| 虎牙直播 | `HuyaDanmaku` |
| 抖音直播 | `DouyinDanmaku` |
| TwitchTV | `TwitchDanmaku` |

**关键文件**:

- **基类与数据结构 (`src/danmaku/base.py`)**：`DanmakuBase` 定义 `start/stop/heartbeat/decode_message` 四个抽象方法与 `_emit()` 回调出口；默认心跳间隔为 45 秒，平台可覆盖。`DanmakuMessageType` 包含 `CHAT/GIFT/ONLINE/SUPER_CHAT`，`DanmakuMessage` 保存类型、用户名、正文、平台数据和颜色。
- **采集器 (`src/danmaku/collector.py`)**：把异步客户端包装为线程化同步接口。`start()` 锚定字幕时间轴，启动独立事件循环的采集线程；需要落盘时另起 SRT 写线程，经有界队列交付聊天消息，避免文件 I/O 阻塞事件循环。全部消息类型均可上报监控，仅有效 `CHAT` 写字幕；`write_srt=False` 表示只监控。`stop(timeout=8.0)` 幂等，限时关闭连接及回收线程，并报告未完成的字幕收尾；`message_count` 提供计数。
- **各平台弹幕客户端 (`src/danmaku/platforms/`)**: 五个 `DanmakuBase` 子类 + 两个私有签名/编解码工具。

| 文件 | 类 | WebSocket 端点 | 关键协议/逻辑 |
| --- | --- | --- | --- |
| `douyin.py` | `DouyinDanmaku` | `wss://webcast100-ws-web-lq.douyin.com/webcast/im/push/v2/` | gzip 解 `PushFrame.payload`→`Response`（protobuf）；`danmaku_signature`（`_xbogus`）生成 `signature`；Cookie 缺失 `await get_ttwid()`；`backup_url` 把 `lq`→`lf` |
| `douyu.py` | `DouyuDanmaku` | `wss://danmuproxy.douyu.com:8506` | 小端二进制帧 + STT 文本协议；`_dispatch` 处理 `chatmsg`（按 `if==1` 粉丝过滤）emit CHAT；心跳发 `mrkl` |
| `huya.py` | `HuyaDanmaku` | `wss://cdnws.api.huya.com` | Tars 二进制协议（`_tars`）；`_make_join_data()` 写 `WSRegisterReq`；`cmdType==7`→`_decode_chat`（HYMessage） |
| `bilibili.py` | `BilibiliDanmaku` | `wss://{host}/sub`（遍历 `host_list`） | 16B 大端帧头；`protover=2` zlib / `=3` brotli 解压；`operation==8` AUTH_REPLY 校验 `code==0`，失败/超时经 `_reject_auth()` + `spider.invalidate_bili_buvid_cache()`；`_auth_watchdog`(8s) 兜底 |
| `twitch.py` | `TwitchDanmaku` | `wss://irc-ws.chat.twitch.tv` | 纯 IRC；匿名 `justinfan{random}` 连接；`PING`→`PONG`，解析 PRIVMSG emit CHAT |
| `_tars.py` | （私有）Tars 编解码器 | — | 虎牙用极简 Tars：`TarsInputStream` / `TarsOutputStream`，头字节高 4 位 tag、低 4 位 type |
| `_xbogus.py` | （私有）X-Bogus 签名 | — | 抖音弹幕用：`generate_xbogus`（RC4 + 自定义 base64）、`danmaku_signature(room_id, unique_id)` |

- **弹幕监控枢纽 (`src/danmaku/monitor.py`)**: `DanmakuMonitorHub`（进程单例，经 `get_hub()` 惰性创建）聚合各房间弹幕事件——`room_started/room_connected/room_closed/room_stopped/room_message`，内存快照 `snapshot(since=0)` 供 Web API 消费，并写 JSONL 边车 `logs/danmaku_monitor.jsonl`（5MB 轮转）。监控异常捕获并留痕，避免打断录制；含 10s×6 桶速率窗与每秒 ≤10 条采样折叠。
- **SRT 字幕写入 (`src/danmaku/srt_writer.py`)**: `SrtWriter` 按 `segment_seconds` 分片输出 `{base}_{seg:03d}.srt`（单文件模式 `{base}.srt`），时间轴以采集启动的 `time.monotonic()` 为基准，按相同分片时长重置片内时间，不读取 FFmpeg PTS，也不保证与媒体精确同步；`write()` 持 `threading.Lock` 写条目并 flush。
- **传输与访客凭据**：复用 [网络与凭据](network.md) 的 WebSocket 客户端与类型化缓存，字幕和协议实现不另建一套连接/缓存管理。
- **抖音弹幕协议 (`src/proto/`)**: `douyin.proto`（Proto3）定义 `Response/Message/ChatMessage/GiftMessage/...` 等；`douyin_pb2.py` 为 protoc 生成（DO NOT EDIT），`douyin_pb2.pyi` 为基于pyright 类型存根。抖音弹幕解析链路：`PushFrame.payload`（gzip 后 `Response`）→`Message.payload`→`ChatMessage`。

故障排查见 [FAQ](../faq.md)，停止竞态、字幕转义和认证回归约束只在 [弹幕已知坑](../agent-reference/known-pitfalls.md#弹幕采集与-srt-写入) 维护。

> 返回[架构导航](README.md) · [文档导航](../README.md)。
