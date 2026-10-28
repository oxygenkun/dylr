# 已知坑明细（自 AGENTS.md 外迁）

> 2026-10-23 自 [`AGENTS.md`](../../AGENTS.md)「已知坑（避免回归）」与「本机 y123 部署补丁边界」外迁。
> **语义**：本文件是按主题的回归细节、约束句、回归锁与实测依据；根文件只保留主题索引和「修改前先读」的路由句，不复制约束句。
> **用法**：修改对应行为前先读下方对应小节，并查看生产实现和已有回归用例。两侧小节标题逐字一致，改名须同步。
> 新增主题须同批补根文件索引；一次性实测读数仍写入 [实测数据与证据明细](measured-evidence.md)，本文件不复制数值。

## 录制链与房间线程主循环

录制主链不受 if headers 控制；real_url 为空告警并等待重试，不启动 ffmpeg。注释 URL 的检查必须早于平台解析，房间退出在 finally 注销弹幕。录后脚本命令/异常日志脱敏，超时回收子孙进程并对后续 communicate 限时。

## 流地址探针与可达性校验

- HEAD 非 2xx 走限量 Range GET（200/206 可达）；401/403 按现有复核流程重试，末位候选允许告警交 ffmpeg 决定，但不得绕过协议/内网安全判定。
- 虎牙专用退避、FLV-first、抖动和至少一个主循环的退避窗口必须保留；斗鱼 HLS 优先。HLS 整组排除与候选调序是不同操作。
- UA、proxy、verify 在签名、探针及 ffmpeg 间保持约定一致；UA 修改核对 MOBILE_UA、ffmpeg 默认、room.HEADERS 与 B站 H5。
- 重定向安全校验复用 async_http._build_client 与 stream_select._probe_client 的接线点；不另建客户端绕开，不缓存 DNS 判定。响应钩子不等于建连前防护，不能声称已闭合 DNS 重绑定。
- 响应决定的落地 URL 检查域族、userinfo 和内网地址；失败保留用户原始 URL、剥离 Cookie，下游拼接主机再次校验。sync_http opener 显式协议白名单，不修改进程全局 opener。
- 小红书链接统一 parse_xhs_live_url；房间号只收 ASCII 数字，未开播保留主播信息与监控作业，失败不得伪装未开播或请求 /user/profile/None。

## 弹幕采集与 SRT 写入

- WS 显式 proxy=None；B站 buvid 走真实注册链，AUTH_REPLY 非零须失效缓存并断开，无响应由 watchdog 限时。
- collector.stop 先置停止事件再读 loop；_run 先发布 loop 再查事件，关闭协程限时。stop 在录制轮询循环外并保持幂等。
- SRT 外部用户名/正文转义换行及 -->，保持单块/单时间轴；分段 end 钳在 start 与分段长度间。视频 %03d、音频 %02d 占位剥离须兼容，SRT 三位编号对齐。
- 抖音空 Cookie 动态获取 ttwid；signature 保持上游未编码契约，不顺手 quote。B站 host 轮换仅在候选耗尽或停止后经 _report_close 恰一次上报最终关闭。

## 平台接口、签名与流地址解析

- 斗鱼保留同 token FLV→HLS 候选；数字 rid 用现有提取 helper，新域名同时登记 PLATFORM_HOST 与分发表。
- 抖音数字房号和抖音号均可传 web_rid，不加多余重定向；200 空响应先排查 UA/Cookie，用户接口用 DESKTOP_UA。
- web/enter 空列表只有显式离线 room_status 才静默返回，否则保留 HTML 兜底；roomStore 先解析完整 JSON 片段，失败才补括号。
- hevc_flv_url 保留 codec=h265 标记；utils.get_query_params 返回 list，不与 spider.get_params 的 str 混用。

## 画质档位与选源映射

- 蓝光子档折叠 BD，不塞入 QUALITY_MAPPING；中文档位同步 get_quality_code、web_config.QUALITY_KEYWORDS、main 白名单、web/index.html。
- FLV/HLS 各自按原始请求索引钳制，不复用另一侧已钳下标。虎牙不可用档降级或原画，斗鱼最多回退两档，保持 is_live=True。
- HEVC 替换保留 h264 FLV 备选；保存格式允许集与选源、实际输出容器一致。HLS 被配置剔除且最终无源时提示恢复开关，健康轮不刷告警。

## HTTP 客户端复用与连接管理

- 探针客户端仅单次选源复用，不升级全局缓存或禁用 keepalive；业务请求头逐请求传，避免候选间凭据污染。
- sync_http Session 按线程复用，不跨线程共享；不安全 SSL context/opener 惰性建立。没有生产调用时不宣称已获得性能收益。
- 所有出站读取受上限体系约束：异步在唯一 response 钩子，sync_http 和探针走各自限量 helper；超限走原失败出口，不返回半截内容。新增读取点用超限响应的行为测试验证，不能靠变量名或登记表判定安全。

## 并发、事件循环与锁

跨循环淘汰 AsyncClient 不在错误循环创建/调度 aclose；沿用现有释放策略，避免永不完成协程与 RuntimeWarning。Qt QApplication 只在主线程创建；legacy Tk 进程不得混入 Qt 托盘。线程死锁、段错误先取证再归因，不预判为环境噪声。

## ffmpeg 命令构造与容器格式

输入、路径、执行、平台分发复用现有构造入口，不在各平台复制命令。reconnect 选项在 -i 前且紧跟值，HLS 去 reconnect_at_eof；segment_format 经 SEGMENT_FORMAT_BY_SUFFIX 查表。录制命令省略可选 thread_queue_size，重新引入前验证支持版本。操作与测试见 ffmpeg skill。

## 日志、控制台与后台模式

Web 安全拒绝启动必须早于 stdio 重定向，保留日志兜底；重定向后重建 loguru 控制台 sink。logger.add 前检查无控制台的空 stderr；异常日志带类型和上下文并脱敏，房间上下文走 extra[room]。退出状态按进程实际结果判断。Web 服务在主线程运行，由 uvicorn 处理退出信号；finally 保留后处理、ffmpeg 与 HTTP 连接池清理。

## i18n 文案、目录与占位符

- 中文直接使用源码原文，仅维护 `i18n/en_US.json` 英文翻译，无需编译；默认与翻译缺失时均回退中文；前端内嵌目录按对应 UI 文案同步。日志用 i18n.tr(常量模板, **kw)，不先 f-string 插值，形参与占位符同名。
- 提取器看不全 push 正文、间接模板和 raise 文案，新增可见串核对目录覆盖测试；错误产生点翻译，配置键、正则和机器码不翻译。删孤儿键需核对 AST 调用与测试夹具。
- tr 的容错包括 AttributeError / TypeError，不能让翻译异常盖掉原故障。Web 语言切换同时更新后端与前端文案。

## 配置键名、布尔口径与 SSL 验证

- 配置键不含 = / :，读取 utf-8-sig，键名对比大小写归一；旧迁移键不盲补。补写原子、持锁、幂等，写回结果可被 strict parser 重读。
- 容错读取用 config_read.tolerant_config_parser。utils 中该依赖保持函数内延迟导入，避免循环依赖。
- SSL 平台覆盖经 http_config，异常平台只追加不删除用户项；全局已禁用校验时不重复应用覆盖。

## 凭据与敏感配置脱敏

写入配置或日志前一律脱敏：URL / 代理经 utils.mask_credentials()，Web 敏感键按「节白名单 + 键名正则」判定，`'***'` 掩码跳过是写入侧唯一防线。改出站异常日志时用带凭据的异常输入验证输出，不能靠变量名扫描或例外计数判定安全。

## 类型检查、注释与静态检查

按 Python >= 3.14 与 Ruff（`pyproject.toml` 为配置事实源）执行。Windows 专属 API 只在对应平台运行；不为旧类型检查器改宽业务注解。审查声称语法错误时先用项目解释器编译具体片段，不凭旧语法知识改代码。

## 热路径性能（数据结构 / 正则）

主循环成员检测用 set，保序去重用 dict.fromkeys，确需顺序的队列保持 list；高频正则模块级编译或按 key 有界缓存，不把热路径退回重复线性扫描。

## 构建产物、依赖与运行时基线

- 外部资源下载元数据统一维护在 `src/external_resources.json`，不在安装器、构建脚本或测试中复制来源清单；只按配置直接下载，保留哈希/签名校验，不调用包管理器、不探源、不降级为 TOFU。
- 发布运行时必须 fail-closed 钉定；来源、签名和平台变更直接核对统一资源配置及受影响的下载路径，不能把 CI 实测哈希直接回填成信任基线。发布、运行期安装、JS 三类校验互不代替，新增远程执行代码须归类。
- Linux 发布与运行期直下共用资源配置，版本更换同步钉定；运行期不 import build_exe。仅安装实际使用的 ffmpeg / node 可执行文件，校验或解压失败不覆盖原安装；Docker 使用镜像内运行时，禁止自动下载。
- Apple Silicon 优先现有原生 ffmpeg，PATH 判定使用注入前快照并按 realpath 排除自身；standalone 孪生规则同步。
- 主线安全修复核对 standalone 的协议、内网、脱敏、终止、布尔与签名副本，报告同步或不适用原因。
- Docker ARG 在使用前声明；compose 保持 pull_policy: build，不恢复 latest 拉取兜底。不在不可信输入工作流中运行浮动 ref 的第三方动作。

## 装饰器与平台兜底契约

返回 dict 用 trace_error_decorator，返回 str/tuple/None 用 trace_error_decorator_or_none，调用方解包前判空。说明注释放装饰器上方是风格约定；注释不会改变 Python 的装饰器绑定。平台 JSON 兜底走 _loads_dict / _safe_loads 和现有取值 helper，裸 loads 棘轮不得扩大；装饰器变化须核对熔断失败样本语义。

## Web 面板与接口层

- Web 路由走 _route，模型走 web_models.parse，保留请求校验、线程池派发与 422 语义；不恢复 FastAPI/pydantic。房间表格保持受作用域约束的固定布局与窄屏横向滚动。
- 画质写回共用 update_room_quality，刷新编辑器快照和配置显示源，不把子进程旧设置当现值；显示名查表前去序号前缀。

## 测试质量与审查协作流程

测试隔离、变异和平台覆盖统一见测试节。审查结论需核对当前代码和可复现行为，不能因风格偏好误报语法；环境失败与代码失败分别报告，不能未经取证就忽略崩溃。

## 本机 y123 部署补丁边界

shutdown_manager 管理的录制/转换进程只发一次停止信号并等待，不再进入 terminate/kill 兜底；两个登记表注销同步。关注 feed 只作正向直播提示，缺失或失败不解释为关播。修改时运行 test_local_migration、test_douyin_follow、test_graceful_shutdown 对应用例。
