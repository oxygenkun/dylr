# AGENTS.md

## code style and dev environment
- use modern python type hint (>=python3.14, prefering built-in type or keyword instead of types in typing module)
- use `uv` to manage project and python dev tools
- 考虑函数、类的组织结构，保证代码整体布局有序
- 测试应适配正式代码的设计，不要为测试增加兼容层或测试专用接口
- 注释只解释代码难以表达的原因、约束和必要用法，保持简短；不复述代码，不记录修复流水账，不要求每个模块、函数都有说明，不设数量或密度下限。允许直接删除或改写重复、过时、错误的注释。Python 文档字符串遵循 PEP 257，统一使用 """..."""

## 项目结构

[目录参考](docs/agent-reference/project-structure.md)。运行时代码统一在 `src/`，维护与构建工具统一在 `tool/`；CLI / Web 入口分别为 `src.main:main`、`src.web:main`，源码使用 `python -m src.main` / `src.web` 启动。

## 项目级 Skill

下表与 `.agents/skills/` 的一级技能目录一一对应；触发场景与各 skill 的 description 同步。新增、删除或改名时同批更新对应表格和文档引用。

| Skill | 路径 | 触发词 / 场景 | 覆盖领域 |
| --- | --- | --- | --- |
| DLR 开发手册 | `.agents/skills/dlr-dev-playbook/` | 新功能设计、测试编写、运行日志排障、跨平台故障或语法审查 | 按场景加载参考步骤 |
| ffmpeg 命令构造 | `.agents/skills/ffmpeg-command-construction/` | 修改 ffmpeg 录制命令、保存类型、输入输出参数或分段容器映射 | 构造入口与专项验证 |
| 平台选源流程 | `.agents/skills/platform-source-selection/` | 修改平台解析、选源、探针、画质映射或新增平台 | 选源与解析配方 |

`.agents/skills/` 必须入库：gitignore 用 `.agents/*` + `!.agents/skills/` 反选，不能忽略整个 `.agents/` 后再反选；Docker 仍排除整个 `.agents/`。技能 shell 脚本由该目录 `.gitattributes` 钉 LF。

## 依赖管理

- 项目按源码运行，`[tool.uv] package = false`，不构建或安装项目自身。`pyproject.toml` 声明依赖、`uv.lock` 锁定；CI / Docker 用 `--locked`。基础安装 `uv sync --locked`，开发加 `--extra dev`，打包再加 `--extra build`；不以 `--dev` 替代现有 dev extra。
- `requirements.txt` 是基础依赖的 pip 兼容清单，与 project.dependencies 同步。比对时去行内注释、归一包名与规格集合；dev / build 依赖不放回基础清单。
- 安全下限以依赖声明和审计为准；新增或抬升依赖下限跑 `deps-audit`，不随意加漏洞忽略项。pip-audit 属审计工具，不进运行时依赖。
- protobuf 上限放宽前先用兼容的 protoc 重新生成，禁止手改 `douyin_pb2.py`；引用的动态消息字段与现有 `.pyi` 同步。
- 收集期 ModuleNotFoundError 先核对 venv 与锁文件，不改业务代码掩盖缺包；运行 pytest 需要基础依赖及 dev extra。

## 测试

- 测试配置在 `pyproject.toml`；pytest 将未豁免告警作为错误，要求 warnings summary 为空。只在全局配置中有来源说明地忽略第三方告警，不过滤项目自身告警；协程 RuntimeWarning 修根因。
- pytest 与 `tool/check_coverage.py` 仅在需要 commit 时执行；日常编辑和交付不自动运行测试或覆盖率检查。测试可以随改动编写，待提交前运行；未执行项如实报告。
- 新测试优先放在对应模块的 `tests/test_<module>.py`；跨模块行为用清楚的专题名。测试必须驱动生产实现，不在测试中重写锁、缓存或限流逻辑。
- 安全测试验证行为或稳定不变量，不用钉旧变量名/旧文案的否定正则冒充安全保障。
- 前端测试使用 Node 内置 `node:test`，无需 npm。新增 `tests/frontend/*.mjs` 同批添加 Python 包装，并登记 `.github/workflows/ci.yml` 的「Gate frontend tests not skipped」node-id 清单；不另建清单。

### 测试编写强制约定

- 环境变量用 `monkeypatch.setenv/delenv`，不用 `patch.dict(os.environ)` 整体回写环境。
- 替换 stdlib 属性时浅拷贝为 `types.SimpleNamespace(**vars(module))`，只换被测模块的引用，禁止改全进程共享的 os / subprocess / time。替身还须按本用例对象或路径收窄，其他调用透传。
- patch main 的 Popen 使用类；需支持注解求值时定义 `__class_getitem__`。不要把 Popen 换成 lambda。
- 原子写回的只读测试，对目标路径的 replace 注入 PermissionError，其他路径透传；不能只靠 chmod。
- 测试输出用 tmp_path，跨进程用受控临时目录；不用会话共享目录。live collector 的手跑输出 `tests/_out_live` 会被会话收尾清理，需留存先复制。
- 双模 collector 模块级无网络、sleep、清理或 exit；执行体放 main 守卫。数值参数先取原文再 `.isdigit()` 校验，不能只排除 `-` 前缀；清理只认本平台前缀并先判类型。收集须秒级返回，不强求手动脚本都有离线用例。
- 并发测试 `join(timeout=...)` 后在同一函数断言线程 `not is_alive()`；平台分流替身覆盖 Windows 与 POSIX，不靠 skip 跳过可模拟分支。
- 本地化系统工具的子进程输出按字节比较；涉及码页的用例须能单文件运行。已知 UTF-8 文本协议按自身契约处理，不一概禁用文本模式。
- `pytest.importorskip` 探测真正使用的子模块并传 `exc_type=ImportError`；不能靠整模块 skip 伪装通过。
- 保留 conftest 的凭据缓存、弹幕 hub、恒等翻译隔离；不用静默跳过 fixture 掩盖缺依赖或污染。

### 测试收尾清理临时脚本

优先用内联检查或 tmp_path；一次性脚本可放系统临时目录，创建时记路径、失败时也清理。只删除本次创建的明确文件，不按 tmp/mock 前缀全仓删除，不顺手清缓存、git clean 或删除运行期数据。safe-delete 处置见风险控制，收尾用 `git status --short` 核对并列明无法清掉的产物。

## 构建命令

```bash
uv sync --locked --extra build
uv run --locked --extra build python tool/build_exe.py --smoke
uv run --locked --extra build python tool/build_exe.py --dual
```

其他参数先看 `tool/build_exe.py --help`。镜像通过 APP_VERSION 注入 pyproject 版本，`docker compose up -d` 本地构建；不提交运行时配置。

### 打包与冒烟测试语义（tool/build_exe.py）

- CLI / Web 两入口共享 `_internal/`；config、ffmpeg、node 与 exe 同级。JS 签名资源按模块路径打入 `_internal/`。
- 冒烟结果按 `FATAL_MARKERS` 与进程状态判定；headless 特定良性报错只允许精确豁免，不宽泛吞异常。
- zip 名整体拼接，不能用 with_suffix 截断带点版本；拒绝路径分隔符，双产物须名称互异且均存在。`--dual` / `--no-zip` 互斥；陈旧发行 zip 的清理由构建入口自身负责。

### CI / workflow 约定

- 网络安装重试复用 `.github/actions/retry`，smoke_test 的配置畸形统一在 load_config 返回 rc=2，调用使用 fail_fast_codes；不把失败改成 warning/0 让 CI 变绿。
- ci.yml 与 build-release.yml 的 python_build / node_version 同步；动作版本按实际配置维护，第三方非认证动作使用核验过的不可变 SHA，不凭记忆填 ref。
- 变更发布矩阵时同步对应测试与产物数量校验。Linux 家族用 startsWith；YAML 行首 `!` 表达式需引号。
- 多架构镜像用原生双 runner 构建，再按提升到 job outputs 的 digest 合成；不用 QEMU 承担全量跨架构构建。只由版本 tag 发布，凭据仅 GITHUB_TOKEN，不增加重写 latest 的调度入口。
- 发布失败由 release-guard 回收空 Release，不删 tag、不关闭产物缺失检查。
- 本地工具、运行期数据排除项同步 gitignore / dockerignore / Ruff / coverage；`.agents/skills/` 入库例外见技能节。覆盖率主任务与 concurrency 配置均用 exclude_also 并保持同源。
- Docker 只支持 CLI / Web：不加桌面依赖或桌面系统库、桌面入口或 compose profile，uv.lock 必须进上下文。uv 固定版本及摘要，锁定安装，运行阶段不带构建工具，FFmpeg 独立阶段构建。
- deps-audit 阻断 Python 依赖漏洞；Trivy 镜像审计维持 advisory，升级阻断需维护者明确决定。
- 跑 pytest 的 job 安装基础依赖与 dev extra；维护脚本缺可执行文件时明确降级，不阻断后续独立步骤。

## 检查时机与命令

- 日常按改动需要做静态检查，不要求每次 Agent 执行后运行完整检查清单。
- 只有需要 commit 时才运行 pytest 与覆盖率检查，使用最后一次代码编辑后的结果；没有提交需求时不自动执行。
- 提交前使用项目的基础依赖、dev extra；先采集覆盖率，再检查阈值：

```bash
uv run --locked --extra dev python -m pytest --cov=src
uv run --locked --extra dev python tool/check_coverage.py
```

- 静态检查按需直接运行 Ruff 与翻译同步检查；pre-commit 与 CI 保留各自的提交检查入口。Windows 检查使用 `PYTHONUTF8=1`。

## 完成定义（Definition of Done）

1. 完成授权范围，报告实际检查及未执行项；仅在需要 commit 时运行 pytest（0 warnings）与覆盖率检查，不把编辑前的结果当作编辑后的证据。
2. 录制/选源/ffmpeg/平台解析改动在有条件时对受影响平台做增量真机验证，报告脚本、脱敏 URL、PASS/WARN/FAIL 和适用读数；无外网或无活房间则明确未验证范围及用户复验方法，不声称真机已通过。
3. 保留受影响的回归用例；缺少必要覆盖时补测试，按提交时机运行。
4. 用户可见行为、配置/依赖、架构或长期约定变化更新相应文档；重大变化追加中英 CODE_WIKI 日志。小修、测试、注释不强制多处登记，长期规则只在本文维护。
5. 按清理节核对本次临时产物和 git diff，不动用户已有变更。
6. 最终回复说明改动、验证和实质限制；不强制写工具私有记忆、每日流水账或固定回滚模板。

## 流程编排技能的使用边界

新功能或产品行为不清楚时，先给足以决策的简短方案，必要时一次确认；已明确的任务、修复、重构、配置与文档改动直接推进，不重复审批。用户明确要求治理式流程时再加载对应技能并确认 run 范围与 artifact root，不把技能安装目录当产物目录。设计不强制提交，不依赖未安装技能。

## 并发与线程模型

### 基础模型

每房间独立线程，以各自 asyncio.run 驱动异步请求；不存在共享全局事件循环。

### 调度中枢 src/scheduler.py

- 并发由 ConcurrencyScheduler 管理：最大同时录制数为 0 时动态调速，非 0 时固定并发；动态容量不低于配置下限。ResizableSemaphore 允许 0 暂停，缩容不回收在用槽位。
- host 熔断 open→half-open 只放一个带租约探针，超时可重授，避免未上报轮次永久占住探针。计数增量维护，不在持锁热路径反复 sum(deque)。
- start_record 先初始化 record_host，再在平台分派前 allow 预检；网络与录制信号量保持分工，录制槽必须在 Popen 前 acquire，启动及运行全部包在 try/finally 中释放。

### 录制结果反馈约定（src/main.py / src/stream_select.py）

ffmpeg 按退出码上报成功/失败，禁止轮末无条件成功。解析成功和直下成功也须反馈 host 样本；快速失败按实际 `-i` 地址标记退避，成功只清实际成功地址，白名单由 stream_select 维护，不扩大到斗鱼。

### 锁的强制约定

- 异步锁按当前事件循环缓存/重建，不建跨循环共享单例。线程锁的种类与持有范围以当前源码为准，不靠历史清单推断。
- 不跨 await 持有线程锁；异步请求去重复用 cookie_cache 的 singleflight，缓存状态在零 await 的短临界区读写。RLock 仅解决同线程重入，不能提供协程间互斥。
- 锁所有权在函数内部或调用方明确选一处；变更时搜索全部调用点，避免 Lock 嵌套死锁。

## 关键约定

- 版本唯一声明在 pyproject；入口直接读取 pyproject.toml，Docker 经 APP_VERSION 注入；文档版本按需同步。
- 布尔解析复用 config_bool.parse_config_bool，读取写回复用 config_io.read_config_bool；前端 parseConfigBool 保持同一 token 集合，未知值走默认且不覆写用户原文。
- 改公共符号前用 rg 搜索调用点、测试、资源和 standalone 孪生实现；删符号后不留悬空调用。src/main.py 内部的 main 是函数，不作模块前缀使用。
- 外部可执行资源的下载地址、版本与校验值统一维护在 `src/external_resources.json`；更新采用上游公布的哈希或已钉公钥签名，不增加独立检查脚本或重复配置清单。运行期仅直接下载，不调用包管理器、不维护 TOFU/备用源；Docker 使用镜像内组件。
- JS 文件增删与 `src/external_resources.json` 的 `javascript_sha256` 同步；migu.js 返回完整签名 URL，调用方不再拼过时参数。
- 启动缓存清理只限程序顶层与 src 直接含 .py 的目录下 pyc/pyo；不递归全仓。基线用版本 + 路径/源码内容哈希，不能用 size/mtime；冻结或开关关闭时零 I/O，基线失败仍清理并告警。
- 停止时日志归档复用 log_archive.archive_runtime_logs；Windows 改名前关闭句柄，测试按现有开关早退，不重开测试对真实日志的副作用。

## 已知坑（避免回归）

按主题的约束句、回归锁与实测依据集中在 [已知坑明细](docs/agent-reference/known-pitfalls.md)；修改对应行为前先读该主题，并查看生产实现和已有回归用例，本文件不再复制细则。

- 录制链与房间线程主循环
- 流地址探针与可达性校验
- 弹幕采集与 SRT 写入
- 平台接口、签名与流地址解析
- 画质档位与选源映射
- HTTP 客户端复用与连接管理
- 并发、事件循环与锁
- ffmpeg 命令构造与容器格式
- 日志、控制台与后台模式
- i18n 文案、目录与占位符
- 配置键名、布尔口径与 SSL 验证
- 凭据与敏感配置脱敏
- 类型检查、注释与静态检查
- 热路径性能（数据结构 / 正则）
- 构建产物、依赖与运行时基线
- 装饰器与平台兜底契约
- Web 面板与接口层
- 测试质量与审查协作流程
- 本机 y123 部署补丁边界
