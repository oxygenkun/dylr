# 部署

[文档导航](README.md) · [配置说明](configuration.md) · [构建与发布](build-and-release.md) · [README](../README.md)

## 运行包命名

Releases 每个平台提供 **lite**（不含内置 ffmpeg/node，首启尝试自动获取）与 **full**（内置运行时）两包。附件名的平台段由 `build_exe.make_zip()` 取 `sys.platform` 经 `{"win32": "windows", "darwin": "macos"}` 归一（其余归 `linux`），架构段才是 `platform.machine().lower()` 的实际取值：

| 运行平台 | 附件名 |
| --- | --- |
| Windows x64 | `dylr-v<版本>-windows-amd64-lite.zip` / `-full.zip` |
| Linux x86_64 | `dylr-v<版本>-linux-x86_64-lite.zip` / `-full.zip` |
| Linux arm64 | `dylr-v<版本>-linux-aarch64-lite.zip` / `-full.zip` |
| macOS（Apple Silicon） | `dylr-v<版本>-macos-arm64-lite.zip` / `-full.zip` |

Linux arm64 的架构段写作 `aarch64`，即 `platform.machine().lower()` 的取值，不是 `arm64`；该包由四平台发布矩阵产出。lite 包缺少 FFmpeg / Node.js 时按统一资源配置直接下载，失败后可手动安装，不调用系统包管理器。

## Docker 前置要求

- [Docker](https://docs.docker.com/get-docker/)
- [Docker Compose](https://docs.docker.com/compose/install/)

## 快速启动

```bash
git clone https://github.com/oxygenkun/dylr.git
cd dylr

# 在 config/URL_config.ini 中添加直播间地址
docker compose up -d
docker compose logs -f
```

容器内 FFmpeg 与 Node.js 由镜像自带，无需挂载本地 `ffmpeg/`、`node/`；`config/`、`downloads/`、`logs/`、`backup_config/` 通过卷挂载持久化。

## 本地构建

```bash
docker build \
  --build-arg APP_VERSION="$(python -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")" \
  -t oxygenkun/dylr .
docker run -d -v ./config:/app/config -v ./downloads:/app/downloads \
  -v ./logs:/app/logs -v ./backup_config:/app/backup_config oxygenkun/dylr
```

`APP_VERSION` 缺省时镜像 `LABEL version` 固化为空串，因此本地构建建议显式传入。镜像构建细节见 [构建与发布](build-and-release.md#docker-镜像)。

## 拉取预构建多架构镜像（GHCR）

发布工作流在每次推送 `v*` tag 时，由原生 amd64 与 arm64 两个 runner 各构建并推送一层，再按 manifest digest 合成多架构清单；同一条 `docker pull` 在 x86_64 与 arm64 主机上都会拿到本机架构的层。

```bash
# 镜像由当前仓库 owner（oxygenkun）发布，GHCR 命名空间一律小写
docker pull ghcr.io/oxygenkun/dylr:latest
docker run -d -v ./config:/app/config -v ./downloads:/app/downloads \
  -v ./logs:/app/logs -v ./backup_config:/app/backup_config \
  ghcr.io/oxygenkun/dylr:latest
```

- 标签形态：`latest` 与 `vX.Y.Z`；`latest` 只随 `v*` tag 产出，不开放手动覆盖。
- 匿名拉取要求该 GHCR 包可见性为 **public**，需在仓库设置中人工开启。
- 与 `docker-compose.yaml` 的 `image: oxygenkun/dylr:latest` 刻意不同源：compose 走 `pull_policy: build` 就地构建，不拉取 registry 同名镜像；用 GHCR 镜像请按上面 `docker run`。

## 运行模式

`docker-compose.yaml` 只有一个服务、唯一入口 `python -m src.main`；是否开面板由 `config/config.toml` 的 `[Web] web_enable` 决定：

```bash
# 纯录制（默认；web_enable = false）
docker compose up -d

# 面板 + 录制：web_enable 设为 true 后，同一命令启动，浏览器访问 http://localhost:8000
```

Docker 仅支持 CLI / Web。

> ⚠️ Web 模式：面板默认监听 `127.0.0.1`，容器内必须在 `config/config.toml` 的 `[Web]` 节设置 `web_host = 0.0.0.0` 才能从宿主机访问；同时建议开启 `web_auth_enable = true` 并配置 `web_password`。

## 数据挂载

```yaml
volumes:
  - ./config:/app/config:rw                # 配置文件目录（必需）
  - ./downloads:/app/downloads:rw          # 录制文件目录（必需）
  - ./logs:/app/logs:rw                    # 运行日志目录
  - ./backup_config:/app/backup_config:rw  # 配置备份目录
```

## 端口映射

唯一服务恒定映射面板端口；`web_enable = false` 时容器内无人监听（连接被拒，不影响录制）：

```yaml
ports:
  - "127.0.0.1:8000:8000"   # 仅 web_enable = true 时实际监听，默认只暴露给宿主机本地
```

## 环境变量

compose 已注入 `TZ`、`PYTHONUNBUFFERED`、`PYTHONDONTWRITEBYTECODE`、`PYTHONIOENCODING`、`TERM`，含义见 [环境变量配置](configuration.md#环境变量)。

## 镜像特性

- **多阶段构建**：builder 阶段安装依赖到虚拟环境，runtime 阶段精简镜像
- **非 root 用户**：使用 `recorder` 用户运行
- **健康检查**：自动检测 `python -m src.main` 进程是否存活（面板模式进程名相同）
- **资源限制**：默认限制 2 CPU / 2G 内存（可在 docker-compose.yaml 调整）
- **日志轮转**：单文件 50MB，最多保留 3 份
- **内置 Node.js 24 LTS**：用于运行 JavaScript 签名脚本

> 相关：[配置说明](configuration.md) 的 Web 节；构建细节见[构建与发布](build-and-release.md)。返回[文档导航](README.md)。
