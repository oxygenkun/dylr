# syntax=docker/dockerfile:1.7
# =============================================================================
# dylr 容器镜像（多阶段构建）
# 基础镜像：python:3.14-slim-bookworm（Debian 12，最小 Python 运行时）
# 平台与版本清单的事实源是 pyproject.toml，此处不再复述
# =============================================================================

# -----------------------------------------------------------------------------
# 阶段1：构建阶段 —— 只把 Python 依赖装进 /opt/venv
# Node.js 不在此阶段装：它只被 PyExecJS / exejs 在**运行时**跑各平台 JS 签名脚本，
# 构建期引入只会拖慢层缓存；弹幕采集链的 websockets / protobuf / brotli 随
# uv.lock 一并装入 /opt/venv，运行时直接可用。
# -----------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:0.10.9@sha256:10902f58a1606787602f303954cea099626a4adb02acbac4c69920fe9d278f82 AS uv

FROM python:3.14-slim-bookworm AS builder
COPY --from=uv /uv /uvx /usr/local/bin/

# 无缓冲输出便于容器日志、禁止写字节码减小体积；uv 使用系统 Python 与 BuildKit 缓存
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy

# build-essential 用于编译无二进制轮子的依赖（如部分加密 / 解析库）
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 虚拟环境落在中立路径 /opt/venv，对所有用户可读，供运行时阶段整目录复用
RUN uv venv --python /usr/local/bin/python /opt/venv

WORKDIR /build

# 只先拷依赖声明文件：仅依赖变更才重装，源码变更不打断层缓存
COPY pyproject.toml uv.lock ./

# Docker 运行 CLI / Web，仅安装基础依赖。
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# -----------------------------------------------------------------------------
# 自建精简 FFmpeg（2026-10-22）
#
# 为什么不再 apt 装 ffmpeg：Debian 包是「全家桶」构建，实测依赖闭包 232 个包 / 493MB
# 已安装体积，其中 450MB 只服务 ffmpeg 自己——libllvm15 112MB + libgl1-mesa-dri 25MB
# （--enable-opengl/--enable-sdl2 拉进来的 mesa/llvmpipe）、libflite1 27MB（语音合成）、
# libmfx1 27MB（Intel MediaSDK）、libz3-4 22MB、libcodec2 16MB，以及 aom / svt-av1 /
# rav1e / placebo / libass / sdl2 等录制链路永远走不到的可选后端。
# 这里改成自编：原生 demuxer/muxer/decoder/filter 全保留（flv、hls、mp4、mpegts、
# segment；https/tls/rtmp/crypto 协议都在，不需要逐格式白名单），只在外部后端上做减法——
#   * libx264：转 H.264 用（video_postprocess 的 -c:v libx264）
#   * libx265：HEVC 编码，预留给后续站点（本仓当前不主动调用，但必须编进去）
#   * libmp3lame：转 MP3 用（原生只有 mp3 解码，没有编码器）
#   * gnutls：https/tls（不用 openssl：其 3.x 是 Apache-2.0，与 GPLv2 不兼容，
#     configure 会要求 --enable-version3；gnutls 是 LGPLv2.1+，与 --enable-gpl 相容）
# 其余（HEVC 解码、h264 解码、aac、flv/hls 解封装等）都是 ffmpeg 原生实现，不依赖外部库，
# 功能面不减。
#
# 链接口径：libav* 静态链进二进制（只产出一个 /opt/ffmpeg/bin/ffmpeg，不装 .so），编码库按
# Debian 运行时包动态链接——x265 由 Debian 发安全更新，比在镜像里自编一份更省事；代价只是
# 运行阶段多 libx264-164 / libx265-199 / libmp3lame0 三个包（约 19MB）。ffprobe / ffplay
# 运行期不用（全仓 grep 只出现在安装器与打包脚本），故不构建，省一次编译。
#
# 版本与哈希钉定：URL 与 sha256 成对升级，哈希不符即构建失败（fail-closed），口径同
# NodeSource；--disable-autodetect 关掉全部自动探测的外部库，下面再显式开需要的三个。
FROM python:3.14-slim-bookworm AS ffmpeg-builder

RUN --mount=type=bind,source=src/external_resources.json,target=/tmp/external_resources.json \
    resource_env="$(python -c 'import json, shlex; r = json.load(open("/tmp/external_resources.json"))["docker"]; print("\n".join(k.upper() + "=" + shlex.quote(v.format(version=r["ffmpeg_version"])) for k, v in r.items()))')" \
    && eval "$resource_env" \
    && apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        nasm \
        pkg-config \
        libgnutls28-dev \
        zlib1g-dev \
        libx264-dev \
        libx265-dev \
        libmp3lame-dev \
        curl \
        xz-utils \
    && curl -fsSL "${FFMPEG_URL}" -o /tmp/ffmpeg.tar.xz \
    && echo "${FFMPEG_SHA256}  /tmp/ffmpeg.tar.xz" | sha256sum -c - \
    && mkdir -p /usr/src/ffmpeg \
    && tar -xJf /tmp/ffmpeg.tar.xz -C /usr/src/ffmpeg --strip-components=1 \
    && cd /usr/src/ffmpeg \
    && ./configure \
        --prefix=/opt/ffmpeg \
        --disable-doc \
        --disable-ffplay \
        --disable-ffprobe \
        --disable-debug \
        --disable-autodetect \
        --enable-gpl \
        --enable-libx264 \
        --enable-libx265 \
        --enable-libmp3lame \
        --enable-gnutls \
        --enable-zlib \
    && make -j"$(nproc)" \
    && make install \
    && strip /opt/ffmpeg/bin/ffmpeg \
    && rm -rf /usr/src/ffmpeg /tmp/ffmpeg.tar.xz /var/lib/apt/lists/*

# -----------------------------------------------------------------------------
# 阶段2：运行阶段 - 最小化运行镜像
# -----------------------------------------------------------------------------
FROM python:3.14-slim-bookworm

# 默认时区，可被 --build-arg TZ / .env 覆盖
ARG TZ=Asia/Shanghai
# 版本号唯一事实源是 pyproject.toml 的 [project].version，构建时经 --build-arg APP_VERSION 注入；
# docker-compose.yaml 从 .env 读同名变量，不设则 LABEL version 为空（镜像内版本仍由
# main.py / src/web_api.py 经 pyproject.toml 在运行时读取）。本地 / CI 取值：
#   docker build --build-arg APP_VERSION="$(python -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")"
# MIN-14（2026-09-21）：ARG 必须声明在**使用它的 LABEL 之前**——Dockerfile 按行做变量替换，
# 顺序颠倒会让 version 标签固化为空串、--build-arg 完全不生效。
ARG APP_VERSION

# 维护者与项目 URL 与 pyproject.toml [project.urls]/authors 同源（oxygenkun）；上游原作者 ihmily
# 的署名保留在 README「上游项目」行与 index.html 作者信息
LABEL maintainer="oxygenkun <oxygenkun.1@gmail.com>" \
      version="${APP_VERSION}" \
      description="支持抖音、TikTok、YouTube等60+平台直播录制工具" \
      url="https://github.com/oxygenkun/dylr"

# 运行时行为：无缓冲、不写字节码、UTF-8 输出、时区、彩色终端、venv 入 PATH
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOUYIN_SKIP_RUNTIME_CHECK=1 \
    PYTHONIOENCODING=utf-8 \
    TZ=${TZ} \
    TERM=xterm-256color \
    PATH="/opt/venv/bin:$PATH"

# 运行时依赖：ffmpeg（录制与转码）、tzdata（配合 TZ）、curl（取 NodeSource 脚本）、
#   procps（提供 pgrep，供下方 HEALTHCHECK 判活）、ca-certificates（HTTPS 抓取根证书）、
#   nodejs 24 LTS（PyExecJS / exejs 跑各平台 JS 签名脚本；2026-08 实测兼容 Node 24.19.0，
#   全部签名脚本 + migu.js 重写版通过；应用启动不下载运行时）。
# apt-get upgrade -y：牺牲一点可重现性换及时安全补丁。
# NodeSource 脚本「先下载 → 校验 SHA256 → 一致才执行」：原写法 `curl -fsSL <url> | bash -`
#   无哈希校验，源站或 CDN 被替换即以 root 权限在构建期执行任意代码（构建期无运行时隔离，
#   危害等同生产 RCE）；此处固定校验值，配置统一在 src/external_resources.json。
# 下载版本、地址和 SHA256 统一维护在 src/external_resources.json 的 docker 节。
# ffmpeg 本体改由阶段1 自编（原因与配置见阶段1 的「自建精简 FFmpeg」段），这里只装它按
# so 名依赖的三个编码库：libx264（H.264 转码）、libx265（HEVC 编码，预留）、libmp3lame
# （MP3 转码）。三者合计约 19MB，取代原先 apt ffmpeg 的 493MB 依赖闭包。
#
# Node 只被 execjs 以 `node -`（从 stdin 读脚本）调用，不跑 npm、不编原生插件，故 NodeSource
# 的 nodejs 包里两坨运行期死重量装完即删：/usr/include/node（node-gyp 头文件，67MB）与
# npm + corepack（约 17MB）。删除必须放在 apt-get upgrade 之后——否则升级 nodejs 会重新解包
# 把这两坨带回来；紧跟的 `node -e` 自检保证删完 node 本体仍可执行。
RUN --mount=type=bind,source=src/external_resources.json,target=/tmp/external_resources.json \
    resource_env="$(python -c 'import json, shlex; r = json.load(open("/tmp/external_resources.json"))["docker"]; print("\n".join(k.upper() + "=" + shlex.quote(v.format(version=r["ffmpeg_version"])) for k, v in r.items()))')" \
    && eval "$resource_env" \
    && apt-get update && apt-get install -y --no-install-recommends \
    libx264-164 \
    libx265-199 \
    libmp3lame0 \
    tzdata \
    curl \
    procps \
    ca-certificates \
    && curl -fsSL "${NODESOURCE_URL}" -o /tmp/nodesource_setup.sh \
    && echo "${NODESOURCE_SHA256}  /tmp/nodesource_setup.sh" | sha256sum -c - \
    && bash /tmp/nodesource_setup.sh \
    && rm -f /tmp/nodesource_setup.sh \
    && apt-get install -y nodejs \
    && apt-get upgrade -y \
    && rm -rf /usr/include/node /usr/lib/node_modules /usr/bin/npm /usr/bin/npx /usr/bin/corepack \
    && node -e 'process.exit(0)' \
    && ln -fs /usr/share/zoneinfo/${TZ} /etc/localtime \
    && dpkg-reconfigure -f noninteractive tzdata \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 创建非 root 用户（降低容器被攻破后的影响面；uid/gid=1000 便于卷挂载权限对齐）
RUN groupadd --gid 1000 recorder \
    && useradd --uid 1000 --gid recorder --shell /bin/bash --create-home recorder

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# 自编 ffmpeg（构建细节见阶段1）：libav* 静态链进二进制，编码库走上面装的三个 Debian 包。
COPY --from=ffmpeg-builder /opt/ffmpeg/bin/ffmpeg /usr/local/bin/ffmpeg

# 构建期自检：编码器/协议缺任一就让镜像构建失败，而不是等运行期第一次录制才暴露。
# 判据只钉「存在性」——libx264/libx265/libmp3lame 与 https/tls 是运行期硬需求，
# 具体编码参数的正确性由 tests/test_ffmpeg_reconnect_args.py 与 golden 命令锁覆盖。
RUN ffmpeg -hide_banner -version > /dev/null \
    && ffmpeg -hide_banner -encoders | grep -q 'libx265' \
    && ffmpeg -hide_banner -encoders | grep -q 'libx264' \
    && ffmpeg -hide_banner -encoders | grep -q 'libmp3lame' \
    && ffmpeg -hide_banner -protocols | grep -q 'https'

# 应用代码以 recorder 属主入镜像，实际内容由 .dockerignore 裁剪（那份文件是唯一清单事实源）：
# 留下 main.py / web.py + src/（不含桌面窗口层；包含 javascript/ 签名脚本、platforms/ 平台实现、proto/ 弹幕协议）
#   + web/ 静态资源 + i18n/*.json（直接读取，无需编译）；排除测试与工具
#   （tests/ tool/ typings/ 等）、文档、运行期产物（logs/ downloads/ backup_config/，
#   改由卷挂载提供）、平台二进制与 config/*.ini（含凭据）。
COPY --chown=recorder:recorder . ./

# 预建运行期目录（日志 / 录制产物 / 配置备份），使卷挂载前目录已存在且归属 recorder
RUN mkdir -p logs downloads backup_config \
    && chown -R recorder:recorder /app

USER recorder

# 健康检查：唯一入口 python -m src.main（面板模式也是同一进程）
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD pgrep -f 'python -m src[.]main' || exit 1

# Web 管理面板端口。注意：面板默认监听 127.0.0.1，容器内需在 config/config.toml 的
# [Web] 节将 web_host 设为 0.0.0.0 并设 web_enable = true，宿主机端口映射才能访问到面板。
EXPOSE 8000

# 唯一入口；是否同时启动面板由 config/config.toml 的 [Web] web_enable 决定
ENTRYPOINT ["python", "-m", "src.main"]
