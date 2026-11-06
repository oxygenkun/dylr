# 构建与发布

[文档导航](README.md) · [开发指南](development.md) · [架构导航](architecture/README.md)

使用、下载、源码运行和 Docker 部署见 [README](../README.md)，不在此重复安装步骤。工程约定见 [AGENTS](../AGENTS.md#构建命令)，具体参数与流程以构建脚本和工作流为准。

## 本地可执行文件构建

[build_exe.py](../tool/build_exe.py) 使用 PyInstaller onedir，一个 `Analysis` / `EXE` 配一个 `COLLECT`，将唯一入口打成同一个可执行文件；面板由 `[Web] web_enable` 控制，不再产出单独的 Web exe。

```bash
uv sync --locked --extra build
uv run --locked --extra build python tool/build_exe.py --smoke
uv run --locked --extra build python tool/build_exe.py --smoke --dual
```

- `--no-zip` 只生成目录；`--no-runtime` 不附 FFmpeg / Node，由运行期按需下载。
- `--dual` 在一次 PyInstaller 构建后分别归档 lite 与 full，前者不含外部运行时，后者附带运行时；与 `--no-zip` 互斥。
- 其他选项先运行 `uv run --locked --extra build python tool/build_exe.py --help`。

压缩包命名为 `dylr-v{version}-{os}-{arch}-{lite|full}.zip`（非 dual 普通构建没有变体后缀）。版本取自 [pyproject.toml](../pyproject.toml)，不另外声明。

## 产物与冻结资源

以 Windows full 产物为例；外部运行时在 lite 中省略，用户数据目录按需创建。

```text
dist/dylr/
├── dylr.exe
├── config/
├── ffmpeg/
├── node/
├── logs/
├── downloads/
├── backup_config/
└── _internal/
    ├── pyproject.toml
    ├── src/
    │   ├── external_resources.json
    │   └── javascript/
    ├── i18n/en_US.json
    └── web/
```

`_internal/` 同时容纳源码包和 Python 依赖。隐藏导入补齐延迟加载的 i18n、h2、exejs 和 uvicorn 子模块；桌面库及开发工具按构建脚本排除，不能排除面板需要的 Starlette / uvicorn。

源码运行以项目根目录定位资源；冻结运行区分可写的 exe 同级数据与 `_internal/` 打包资源：

- [state.py](../src/recording/state.py) 定位配置、录制及备份，[logger.py](../src/support/logger.py) 定位日志。
- [ffmpeg.py](../src/runtime/ffmpeg.py) / [node.py](../src/runtime/node.py) 定位外部程序，[resources.py](../src/runtime/resources.py) 读取统一下载清单。
- [i18n.py](../src/support/i18n.py) / [routes.py](../src/api/routes.py) 定位翻译 / Web 资源；JS 路径由 [包初始化模块](../src/__init__.py) 的 `JS_SCRIPT_PATH` 提供。

## 打包产物冒烟

`--smoke` 先准备注释房间 URL，避免空列表阻塞输入，再顺序执行两种模式：

1. CLI：启动同一个 exe，观察窗口内进程应保持存活。
2. Web：临时写入 `web_enable=true`，启动同一个 exe，访问本机面板首页，HTTP 200 后收尾并还原配置。

结果综合进程是否提前退出与 `FATAL_MARKERS` 判定；不能把「输出里没有某个词」当作存活验证。Web 探活还受端口占用和启动速度影响。`--dual --smoke` 先产出 lite/full 两个 zip，再对已含 full 运行时的 `dist/` 发布目录执行冒烟；日志与配置回写发生在压缩之后，不会进入分发包。

## CI 验证

[ci.yml](../.github/workflows/ci.yml) 当前仅 `workflow_dispatch` 手动触发，已无 push / PR 自动触发与路径过滤。手动运行按全量检查语义执行；不能因为存在 `ci-summary` 就声称 PR 会自动拿到新检查结果。

| Job | 职责 |
| --- | --- |
| `setup` | 输出 Python / Node 共享常量 |
| `static` | Ruff 格式与静态检查 |
| `deps-audit` | 审计锁文件依赖闭包并复核声明下限 |
| `test` | pytest、前端测试登记门控、全局/模块覆盖率与 Web HTTP 冒烟 |
| `concurrency-test` | 并发专项与独立覆盖率配置 |
| `integration-verify` | 真实 FFmpeg / Node 环境探测 |
| `build-verify` | lite 打包与单入口冒烟 |
| `ci-summary` | 汇总上游状态 |

依赖按 [uv.lock](../uv.lock) 锁定安装；开发/测试使用 dev extra，构建使用 build extra。网络安装复用 [retry action](../.github/actions/retry/action.yml)，缓存以锁文件为 key。动作 ref、版本常量和当前命令不在文档另抄一份。

## 可执行文件发布

[build-release.yml](../.github/workflows/build-release.yml) 支持推送 `v*` tag，或手动构建（可选 `create_release`）。流程如下：

1. `prepare` 提取版本并校验 tag 一致，输出发布状态与环境常量。
2. 发版路径通过单例 `release-create` 预创建 Release；手动发版先确保版本 tag 存在，避免多平台创建竞态。
3. Windows、Linux x64、Linux arm64、macOS 四 runner 安装 build extra，构建 lite / full 并执行脚本冒烟。
4. 发版路径将各 zip 直接上传到同一个 Release；仅构建路径上传 artifact。
5. `release` 拉回附件，校验四平台共八个归档，生成并附加 `SHA256SUMS.txt`；`release-guard` 回收失败留下的空 Release，不删除 tag。

发布运行时的来源、版本和校验数据复用 [外部资源清单](../src/external_resources.json)，不是 CI 下载结果自动生成的信任基线。

## Docker 镜像

[Dockerfile](../Dockerfile) 将 Python 锁定依赖、独立 FFmpeg 构建和运行阶段分离；运行阶段使用镜像内的 FFmpeg / Node，不携带构建工具或桌面依赖。镜像 `APP_VERSION` 来自项目版本。

[compose](../docker-compose.yaml) 只启动 `recorder` 服务，唯一入口与本地相同；监听、认证、端口和挂载操作见 [部署](deployment.md)。[docker-publish.yml](../.github/workflows/docker-publish.yml) 在版本 tag 发布时使用原生双 runner 构建多架构镜像，按 digest 合成，而不是用 QEMU 承担全量构建。镜像安全扫描维持 advisory，Python 依赖审计仍是阻断项。

文档整体由 [.dockerignore](../.dockerignore) 排除，不因架构拆分进入镜像构建上下文。

> 相关：[部署](deployment.md) / [架构导航](architecture/README.md)；返回[文档导航](README.md)。
