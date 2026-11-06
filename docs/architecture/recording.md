# 录制与调度

[架构导航](README.md) · [文档导航](../README.md)

## 初始化与房间会话

`src/main.py` 只负责启动；`src/recording/engine.py` 的 `initialize_process()` 显式执行运行时检查、配置初始化、退出钩子和备份线程启动，CLI 另外安装信号处理器。导入录制模块不会执行这些启动操作，不需要 `sys.modules` 入口别名或重导出入口函数。

`src/recording/session.py` 管理单房间轮询、直下、FFmpeg 命令、看门狗和录后处理接线；临时解析变量留在函数内。共享配置、集合、计数器、锁和调度器引用归 `src/recording/state.py`，状态展示归 `src/recording/status.py`。完整调用关系见 [架构总览](README.md#工作流程与依赖边界)，文件清单只在 [项目结构](../agent-reference/project-structure.md) 维护。

## 并发调度中枢 (`src/recording/scheduler.py`)

**职责**: 统一管理全局网络并发容量、按平台（host）隔离熔断降级、支持自适应调速与固定并发双模式的可运行时调容信号量系统。

**核心类**:

- **`ResizableSemaphore`**: 可运行时调容的信号量，实现上下文管理器协议。支持 `set_value(n)` 运行时增减容量——增大时唤醒相应数量的等待者，减小时仅降低上限、不强行回收已持有槽位。`__init__` / `set_value` 允许容量为 0（暂停态）。消除旧「销毁重建信号量」的竞态风险。
- **`PlatformBreaker`**: 按 key（host）隔离的熔断器，实现 `closed → open → half-open` 三态状态机。连续失败样本比例超阈值即 open（跳过探测并进入冷却），冷却后经**唯一**探针放行；探针成功恢复 closed、失败重新 open。用于将单平台抖动隔离降级，避免连锁拖垮全局。**探针带租约（`_PROBE_LEASE_SECONDS = 60s`）**：被授予后超时仍未回报时，`allow()` 可重新授予，避免未开播、禁录或退出路径占住 half-open 探针。
- **`ConcurrencyScheduler`**: 调度中枢，整合自适应容量、平台熔断、录制并发上限三大能力。
  - **网络并发容量**：先取 `max(网络线程数配置, min(max_capacity, ceil(活跃数/scale_divisor)))`；错误背压可将其降低，但不低于 `min_capacity`。具体常量和阈值只在实现维护
  - **自适应模式**（默认，`最大同时录制数(0为不限制) = 0`）：容量随活跃任务数动态缩放，错误率反馈驱动温和背压
  - **固定并发模式**（`最大同时录制数(0为不限制) ≠ 0`）：忽略动态调速器与错误背压，网络容量恒为「同一时间访问网络的线程数」（最小 1 槽位，热更新即时生效）
  - **录制并发软上限**：通过 `recording_semaphore` 管控同时 ffmpeg 录制数，默认 0 为不限制
  - `adjust_loop` 守护循环每 5 秒重算容量，取代旧的单向压制 `adjust_max_request`

**关键函数**:

- `host_of(url)`: 从 URL 提取主机名（截到首个 `/`、`?`、`#` 为止，小写、保留端口）作为熔断 key；空串或解析异常统一归 `"unknown"`（互不相关的坏 URL 共享同一熔断 key，粗粒度兜底）
- `allow(key)`: 预检指定 host 是否已熔断，返回 False 时调用方应跳过本轮探测；half-open 态带探针租约（见 `PlatformBreaker`）
- `record_failure(key)` / `record_success(key)`: 按 host 计入成功/失败样本，驱动熔断器状态迁移

**接线点**:

- `notify.record_error/record_success` 接收 host key，分别委托给调度器的 `record_failure/record_success`
- `start_record` 入口在平台分派前做 `scheduler.allow(record_host)` 熔断预检
- `start_record` 解析成功分支（`anchor_name` 非空）上报 `record_success(record_host)`——half-open 探针依赖本轮结果闭环，探针房间进入长时间录制期间同 host 其余房间不再饿死
- `check_subprocess` 的录制循环受 `recording_semaphore` 管控
- `main()` 首轮初始化调度器，`semaphore` / `recording_semaphore` 指向其属性

**线程安全**（2026-08-27 加固）: 配置字段（模式/配置上限/活跃数/错误窗口）由 main 主线程与 `adjust_loop` 守护线程并发读写，`_compute_capacity()` 单次加锁快照全部可变输入、`set_configured_limit()` / `set_dynamic_mode()` 锁内写入（幂等检查与写入原子）；`Lock` 不可重入，所有 setter 在锁释放后再调 `recompute()`，全链路无嵌套持锁。

配置填写与默认值见 [配置说明](../configuration.md)，不另维护默认值表。

**回归入口**: `tests/test_scheduler.py`，覆盖信号量调容、熔断器状态机（含探针租约超时自愈）、自适应容量缩放/下限、固定并发模式、按 key 隔离、录制并发软上限等。

可调用接口直接见 `src/recording/scheduler.py`，不再维护平行伪代码声明。录制结果反馈、槽位释放与锁的强制约定见 [AGENTS](../../AGENTS.md#并发与线程模型)；FFmpeg 构造与容器约束见 [已知坑](../agent-reference/known-pitfalls.md#ffmpeg-命令构造与容器格式)。

> 返回[架构导航](README.md) · [文档导航](../README.md)。
