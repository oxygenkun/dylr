# 网络与凭据

[架构导航](README.md) · [文档导航](../README.md)

## 网络服务 (`src/network/`)

| 模块 | 职责与入口 |
| --- | --- |
| `http.py` | `request()`、`is_reachable()`；按事件循环复用 HTTP 客户端，独占请求由异步上下文管理器关闭 |
| `targets.py` | 地址、IP 与 DNS 判定；`validate_http_target()` 在发送每一跳之前拒绝不允许的目标 |
| `policy.py` | `verify_tls`、`verify_stream_tls`、`stream_tls_overrides` 与 `should_verify_stream_tls()` |
| `credentials.py` | `CredentialCache[T]`；Cookie 与其他凭据共用去重算法，各自保存状态 |
| `proxy.py` | `parse_proxy_url()`、`detect_system_proxy()` 与不可变 `ProxyInfo`；注册表句柄按读取作用域释放 |
| `websocket.py` | `WebSocketClient`；串行发送、心跳、退避重连与一次性失败通知 |
| `urls.py` | 代理地址归一、Cookie 格式化、JSONP 解析与查询参数读取 |

`request()` 保持文本、Cookie、文本加 Cookie、最终 URL 的业务返回契约，请求选项均以关键字传入；`is_reachable()` 固定返回布尔值。客户端构造与清理不依赖 Web 模块。

## 资源与缓存生命周期

普通 HTTP 请求按代理、TLS、HTTP/2、事件循环复用客户端；`return_cookies=True` 的取 Cookie 请求使用独占客户端（普通请求即使带 Cookie 请求头仍可复用客户端）。关闭时只在创建客户端的线程与事件循环释放连接，其他循环的条目只丢引用。

`CredentialCache.get_or_fetch()` 在短线程锁内登记状态，在锁外等待与拉取，通过等待者所属循环交付结果。过期结果、失败、失效期间完成的拉取均不错误回填；关闭循环的孤儿登记会被回收。`wait_timeout` 只限制等待者，factory 自行约束 I/O 超时。Cookie 缓存与平台侧凭据状态仍须同时清理。

`policy.verify_tls` 控制平台 API 验证；引擎直接设置 `verify_stream_tls = not enable_https_recording`，平台覆盖只在拉流侧需要验证时参与裁决。

## 出站校验边界

异步 HTTP 与同步选源探针共用 `targets.py`。request 钩子在发送初始地址及每一次重定向前检查；响应读取上限保持普通响应 8 MiB、解压响应 32 MiB，同步探针保留自己的小响应上限。代理域名使用代理侧解析语义，IP 字面量与已知内部域名仍校验。

DNS 检查与传输层实际解析是两次操作，仍存在重绑定时间窗口；本实现不声称固定了最终连接 IP。独立录制工具保留自己的 HTTP/HTTPS urllib 白名单与重定向前检查。

WebSocket 的连接、串行发送、心跳和重连由 `src/network/websocket.py` 的 `WebSocketClient` 提供，平台协议见 [弹幕与字幕](danmaku.md)。回归边界见 [HTTP 客户端约束](../agent-reference/known-pitfalls.md#http-客户端复用与连接管理)。

通用 WebSocket 客户端的 `proxy=None` 显式直连，也接受平台传入的代理；Twitch 适配器另有系统 HTTP/HTTPS 代理兜底。排查步骤统一见 [FAQ](../faq.md)。

> 返回[架构导航](README.md) · [文档导航](../README.md)。
