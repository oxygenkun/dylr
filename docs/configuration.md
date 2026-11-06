# 配置说明

[文档导航](README.md) · [使用说明](usage.md) · [README](../README.md)

主配置为 `config/config.toml`；完整键位模板见 [config/config.toml.example](../config/config.toml.example)，复制为 `config/config.toml` 后填写。该文件含凭据，切勿提交。

> 配置为 TOML：含中文、括号或 `|` 的节名与键名用双引号包裹，布尔写 `true`/`false`。旧版 `config.ini` 在首次启动时自动迁移为 `config.toml`（原文件改名为 `config.ini.migrated`）；读取时按类型校验，非法值回退默认并记警告；文件改动在下一轮检测循环热加载，无需重启。

## 基础配置

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
"单次录制时长上限(秒,0为不限制)" = 0
```

## 推送配置

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
"关播推送开启(是/否)" = false
```

## Cookie 配置

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
# ... 共 51 个平台 cookie 键，其余详见 config.toml
```

访客类 Cookie（抖音 ttwid、快手 did 等）由 `src/network/credentials.py` 以「归一化网址 + 代理」为 key 做进程级共享缓存（默认 30 分钟 TTL），多直播间并发时不会重复拉取触发风控。

## 授权配置

```toml
["Authorization"]
# PopkonTV 登录后取得的 token（由账号密码自动登录后写回）
"popkontv_token" = ""
```

## 账号密码配置

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
"twitcasting密码" = ""
```

## Web 管理面板配置

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
"web_allowed_hosts" = ""
```

`web_enable` 默认 `false`（纯命令行录制，不监听端口）；Docker 与本地运行一致，只由 config.toml 决定，compose 不注入覆盖变量。8000 端口恒定映射，未开面板时容器内无人监听。

`web_host` 在仓库默认配置中为 `127.0.0.1`（仅本机可访问）。Docker 或需局域网/公网访问时改为 `0.0.0.0`，并务必同时开启 `web_auth_enable` 与 `web_password`。

## 直播间配置

直播间列表保存在 `config/URL_config.ini`。抖音支持以下 5 种直播间/主页地址格式，其余平台按各自域名填写：

```ini
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

- 格式 1/3/5 走网页端接口（支持 VR 直播）；格式 2/4 走 app 端接口。
- 格式 5（`www.douyin.com/user/<sec_uid>`）直接从地址提取 `sec_user_id` 并解析出抖音号，按直播间地址走网页端接口，无需 app 端探测。
- 格式 4 等短链形态会先探测直播间地址，失败则回退到抖音号解析，再走网页端接口。

可选写法：

```ini
# 指定画质（画质,直播间地址）
超清，https://live.douyin.com/745964462470

# 指定画质和主播名（画质,直播间地址,主播:名称）
高清，https://live.bilibili.com/123456，主播: B站主播

# 注释直播间（在地址前加 #）
# https://live.douyin.com/123456789
```

主播改名后的目录、文件与配置同步规则见 [主播名自动更新](architecture/configuration.md#主播名自动更新)。

## 环境变量

| 变量名 | 说明 | 示例 |
| --- | --- | --- |
| `PYTHONUNBUFFERED` | 实时输出日志 | `1` |
| `PYTHONDONTWRITEBYTECODE` | 不生成 .pyc 文件 | `1` |
| `PYTHONIOENCODING` | Python 输出编码 | `utf-8` |
| `TZ` | 时区设置 | `Asia/Shanghai` |
| `TERM` | 终端类型 | `xterm-256color` |

Docker 已按上表注入这些变量，部署说明见 [部署](deployment.md)。

> 相关：[使用说明](usage.md)、[部署](deployment.md)；返回[文档导航](README.md)。
