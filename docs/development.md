# 开发指南

[返回 README](../README.md)

> 开发检查：pytest 与覆盖率仅在需要 commit 时执行；日常编辑不自动运行。现行约定见 [AGENTS.md](../AGENTS.md)，历史更新记录中的已退役工具不作为当前要求。

## 环境要求

- Python >= 3.14
- FFmpeg 和 Node.js：优先使用已有程序，否则按资源配置直接下载到程序目录；失败时自行安装。
- macOS 自动下载 FFmpeg 需要 gpg 验证签名；Docker 已内置运行时。

## 安装开发依赖

```bash
# 安装基础依赖和开发工具
uv sync --locked --extra dev
```

依赖只在 `pyproject.toml` 声明并由 `uv.lock` 锁定。基础依赖使用 `uv add` / `uv remove`；开发和构建工具分别加 `--optional dev` / `--optional build`，对应锁文件变更一并提交。

## 提交钩子

prek 由 dev extra 安装，复用 `.pre-commit-config.yaml`。YAML、TOML 和文件大小检查使用内置钩子；Ruff 使用项目锁定版本。

```bash
uv run --locked --extra dev prek install
uv run --locked --extra dev prek run --all-files
```

每个克隆目录安装一次钩子。已有 pre-commit 钩子时，确认原钩子无额外自定义逻辑后使用 `prek install --force` 替换。

## 代码规范

```bash
# 格式化代码（line-length = 120）
uv run --locked --extra dev ruff format .

# 静态检查（加 --fix 可应用安全修复）
uv run --locked --extra dev ruff check .
```

> **注释规范**：见 [AGENTS.md](../AGENTS.md) 的代码风格约定。

> **质量检查**：Ruff 负责格式化，并检查常见错误、导入排序、现代 Python 写法、冗余代码、遗留调试器和 `noqa` 的有效性；具体规则与例外以 `pyproject.toml` 为准。pytest 要求 0 warnings，不配置常驻类型检查器。检查时机、安全修复与豁免约定见 [AGENTS.md](../AGENTS.md)；日常编辑不自动运行 pytest 或覆盖率检查。

## 测试与覆盖率

测试放在 `tests/`。源码搬迁后，既有文件名仍保留，例如 [test_ffmpeg_proc.py](../tests/test_ffmpeg_proc.py) 与 [test_stream_select.py](../tests/test_stream_select.py) 分别覆盖 [recording/process.py](../src/recording/process.py) 与 [streams/selection.py](../src/streams/selection.py)。前端 `tests/frontend/` 使用 Node 内置 `node:test`，无需 npm；新增用例须同步 Python 包装，并在 [ci.yml](../.github/workflows/ci.yml) 的「Gate frontend tests not skipped」登记 node-id。

安装基础依赖与 dev extra 后，仅在准备提交时采集覆盖率并检查阈值；日常编辑不自动执行：

```bash
uv run --locked --extra dev python -m pytest --cov=src
uv run --locked --extra dev python tool/check_coverage.py
```

全局覆盖率配置在 [pyproject.toml](../pyproject.toml)，逐模块阈值以 [check_coverage.py](../tool/check_coverage.py) 的 `MODULE_THRESHOLDS` 为准；并发专项使用 [.coveragerc-concurrency](../.coveragerc-concurrency)。历史实测读数见 [measured-evidence.md](agent-reference/measured-evidence.md)，不代替当前检查结果。测试隔离、告警与回归约定以 [AGENTS.md](../AGENTS.md#测试) 为准。

> **测试说明**：`tests/test_web_api.py` 的符号链接相关用例（`TestListFiles::test_broken_symlink_skipped` / `test_symlink_outside_skipped`）在无法创建真实符号链接的环境（未开启开发者模式的 Windows、部分沙箱）会自动 `pytest.skip`，属正常现象，不代表代码缺陷。

## 项目文档

- [文档导航](README.md) - 全部文档入口与维护边界
- [目录与文件职责](agent-reference/project-structure.md) - 完整源码树、工具、测试、文档与运行期目录
- [架构索引](architecture/README.md) - 模块说明、依赖关系与设计
- [平台扩展](architecture/platforms.md) - 平台分派、选源与弹幕接线
- [配置说明](configuration.md) - 配置键、模板与热加载
- [使用说明](usage.md) - 录制、画质、弹幕与平台链接
- [部署](deployment.md) - 运行包与 Docker 部署
- [构建与发布](build-and-release.md) - 打包产物、冒烟与发布流程
- [常见问题](faq.md) - 使用与运行排障
- [AGENTS.md](../AGENTS.md) - 工程约定与检查命令

## Web/接口冒烟测试

项目内置通用、零依赖的 Web/接口冒烟测试工具 `tool/smoke_test.py`（纯标准库，无需安装第三方包），可对 Web 管理面板等**运行中 HTTP 接口**做轻量探活。

```bash
# 检查本机 Web 管理面板（默认 127.0.0.1:8000，示例配置见 tool/smoke_web.json）
python tool/smoke_test.py -c tool/smoke_web.json

# 生成 HTML 报告
python tool/smoke_test.py -c tool/smoke_web.json -r smoke_report.html -f html
```

- 配置驱动（JSON）：`url` / `method` / `expected_status` / `timeout` / 请求头 / 请求体 / 响应应包含文本 / 期望 JSON 字段
- 支持 `base_url` 前缀拼接；控制台 / JSON / HTML 三种报告；失败时退出码非 0（可接入 CI）
- 与 `tool/build_exe.py --smoke`（[打包产物冒烟](build-and-release.md)）不同，本工具针对**运行中的 HTTP 接口**做探活，两者互补
- 默认用例 `tool/smoke_web.json` 探活 Web 面板 `/`（首页 200）与 `/health`（`{"status": "ok"}`，由 `src/api/routes.py` 提供的公开探活端点，不受 `web_auth_enable` 支配）
- **已接入 CI**：[ci.yml](../.github/workflows/ci.yml) 的 `test` job 在 pytest 之后通过 [_ci_web_smoke.sh](../tool/_ci_web_smoke.sh) 将运行期配置中的 `web_enable` 置 true，以 `python -m src.main` 受控启动面板、等待就绪，再跑 [smoke_web.json](../tool/smoke_web.json) 断言（经 `.github/actions/retry` 处理网络抖动，失败保留 `logs/web-smoke-*` 工件并使 job 变红），补齐「面板进程能否真正监听并应答」这条 TestClient 覆盖不到的链路

## 添加新平台支持

**视频录制平台：**

1. 在 `src/streams/spider.py` 中添加平台数据获取函数（参考现有平台实现）
2. 在 `src/streams/stream.py` 中添加流地址与画质解析函数；支持画质回采时返回 `actual_quality` 和 `available_qualities` 字段
3. 在 `src/streams/dispatch.py` 登记 URL 准入、平台 resolver 与房间上下文
4. 补充 `tests/` 回归用例，同步使用文档与 [平台架构说明](architecture/platforms.md)；文档入口见 [架构索引](architecture/README.md)

**弹幕录制平台（如需支持弹幕）：**

1. 在 `src/danmaku/platforms/` 下新建 `<平台>.py`，继承 `src/danmaku/base.py` 的 `DanmakuBase`，实现连接/鉴权/消息解析
2. 在 `src/__init__.py` 的弹幕平台注册表中登记（平台名与 `src/streams/dispatch.py` 的 platform 标识一致），由 `get_danmaku_class` / `get_danmaku_collector` 工厂解耦创建
3. 在 `src/streams/dispatch.py` 的 resolver 中填充弹幕参数；`src/recording/session.py` 沿用公共采集器生命周期
4. 补充 `tests/` 回归用例，同步使用文档与 [平台架构说明](architecture/platforms.md)；文档入口见 [架构索引](architecture/README.md)

选源与解析的操作配方见 [平台选源流程](../.agents/skills/platform-source-selection/SKILL.md)。

> 注：弹幕子系统与 `src/streams/spider.py`（视频流地址解析）是平行解耦的两套抽象，`spider.py` 不导入 `src/danmaku/platforms/`。

> 相关：[架构导航](architecture/README.md)、[项目结构](agent-reference/project-structure.md)；返回[文档导航](README.md)。
