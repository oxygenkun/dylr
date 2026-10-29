#!/usr/bin/env python3
# -*- encoding: utf-8 -*-
#
# DouyinLiveRecorder 可执行文件打包脚本（本地 / GitHub Actions 通用）
#
# 一次构建产出两个入口（CLI / Web），共享同一发布目录的依赖与资源；
#     两个 exe 与 main.py / web.py 的对应关系见下方「产物布局」。
#
# 用法：
#     python tool/build_exe.py             # 打包并生成 zip 产物
#     python tool/build_exe.py --smoke     # 打包后额外运行冒烟测试（CI 推荐）
#     python tool/build_exe.py --no-zip    # 只打包不压缩
#     python tool/build_exe.py --no-runtime # 跳过 ffmpeg/node 打包（交由用户运行时自动下载，减小分发体积）
#     python tool/build_exe.py --dual      # 同时生成 lite（无运行时）与 full（下载并打包 ffmpeg+node）两个 zip
#
# 产物布局（onedir + contents_directory='_internal'）：
#     dist/DouyinLiveRecorder/
#     ├── DouyinLiveRecorder{,-Web}(.exe)   两个入口，见上表
#     ├── config/ · ffmpeg/ · node/              运行时资源，一律与 exe 同级（不进 _internal/）
#     └── _internal/                             依赖包 + src/ 及打包资源（i18n/ web/ src/javascript）
#     划分依据：经 __file__ 定位的资源随 contents_directory 收进 _internal/；经 sys.argv[0]/
#     sys.executable 定位的（config/ ffmpeg/ node/）由本脚本在 COLLECT 之后复制到 exe 同级（见
#     copy_external_binaries / _prepare_config_dir），二者经 src/logger._app_root() 收敛到同一目录。
#     多入口共享依赖必须用 .spec 文件（命令行不支持多 Analysis），故本脚本动态生成 spec 后调用 PyInstaller。
#
# 产物：
#     dist/DouyinLiveRecorder/                          发布目录（2 个 exe + 运行时目录 + _internal/ 依赖）
#     dist/DouyinLiveRecorder-v{ver}-{os}-{arch}{suffix}.zip  压缩包（普通构建 suffix 为空，--dual 为 -lite/-full）
#
import argparse
import http.client
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.runtime.installer import install_runtime

APP_NAME = "DouyinLiveRecorder"
DIST_DIR = PROJECT_ROOT / "dist"
RELEASE_DIR = DIST_DIR / APP_NAME
SPEC_PATH = PROJECT_ROOT / "DouyinLiveRecorder.spec"
# 与下方 spec 模板中的 contents_directory 保持一致：冻结后所有资源落在 _internal/
CONTENTS_DIR = "_internal"
IS_WIN = sys.platform == "win32"
EXE_SUFFIX = ".exe" if IS_WIN else ""

# 体积优化：运行期不可达模块的排除清单（每项体积为 2026-09-24 本机 Windows lite 构建实测值，
# 基线发布目录 82.77MB）。进表判据只有一条：能指出它在产物里、且运行期不可达——靠「看着像没用」
# 排除会在某个平台/分支上变成 ModuleNotFoundError（冻结产物里该错误只在运行时炸，测试覆盖不到）。
# 新增排除项应确认运行期不可达，并验证冻结入口。
BLOAT_EXCLUDES: tuple[str, ...] = (
    # pydantic v1 兼容层的 mypy 插件：pydantic/v1/mypy.py 顶层 `from mypy.errorcodes import ErrorCode`
    # 会把整个 mypy 包（0.71MB / 69 个文件）拖进产物，是基线里唯一一处「开发工具链泄漏」。
    # [历史注] pydantic 已随 Web 后端阶段2 移出依赖树与 import 面（本地 venv 仍有残留），本条为纯防御。
    "pydantic.v1.mypy",
    # Pillow 插件：PIL 已不在依赖树里（2026-10-20 随遗留 pystray 托盘摘除，托盘图标改由
    # QPainter 现画），本组条目为纯防御——构建 venv 里 Pillow 仍装着，一旦被重新拉进依赖图，
    # 这几个插件就是最大的一块死重量。容错依据：PIL/ImageFont.py 的 `from . import _imagingft` 与
    # PIL/Image.py 的插件 init() 都是 try/except ImportError，缺失只在真调用时报错，不会在导入期炸。
    "PIL._avif",  # 7.52MB：Pillow 12 自带 libavif，是产物内最大的单个非解释器文件
    "PIL._imagingft",  # 2.07MB：FreeType；全仓无 ImageFont / truetype 调用点
    "PIL._webp",  # 0.40MB
    "PIL._imagingcms",  # 0.26MB
    "PIL._imagingmath",  # 0.02MB
    # uvicorn 的可选实现依赖：web.py 用 uvicorn.Server(uvicorn.Config(...))，http/loop/ws 三项均为
    # 默认 "auto"，其内部就是「try 可选实现 except ImportError 回退」（httptools→h11、uvloop→asyncio、
    # wsproto→websockets）；watchfiles 仅 --reload 用，冻结产物从不 reload。故排除只影响本仓没有的用法。
    "httptools",  # 0.17MB
    "watchfiles",  # 0.61MB
    "uvloop",  # 仅 POSIX 安装；Windows 无此包，写明以免将来换平台构建时漏排
    "zuvloop",
    "wsproto",
    # 开发 / 测试工具链：都不在 requirements.txt 里、运行期不会导入，但一个误加的顶层 import 就
    # 会被静默带进对外分发的 zip（mypy 正是这样进来的）。显式排除把「误收集」变成「不可能发生」。
    "pytest",
    "_pytest",
    "mypy",
    "mypyc",
    "mypy_extensions",
    "basedpyright",
    "black",
    "blackd",
    "blib2to3",
    "isort",
    "coverage",
    "pip",
    "pygments",
    "nodejs_wheel",  # 114MB：本仓 venv 里有而 requirements.txt 没有，误收集即炸体积
    "py",
    "iniconfig",
    "pluggy",
)


# 仅打包运行时读取的 JSON 翻译目录。
def i18n_datas_entries() -> list[tuple[str, str]]:
    return [
        (path.relative_to(PROJECT_ROOT).as_posix(), "i18n")
        for path in sorted((PROJECT_ROOT / "i18n").glob("*.json"))
        if path.is_file()
    ]


# 动态生成的 PyInstaller spec 模板：
# - 两个 Analysis / EXE，共用一个 COLLECT（共享依赖去重）
# - 数据文件只挂在 CLI 的 Analysis 上即可（COLLECT 合并时统一落盘）
SPEC_TEMPLATE = """\
# -*- mode: python ; coding: utf-8 -*-
# 本文件由 build_exe.py 自动生成，请勿手工编辑（修改请改 build_exe.py）
from PyInstaller.utils.hooks import collect_submodules

# 体积优化排除项（清单与每项实测体积见 build_exe.py 的 BLOAT_EXCLUDES）
excludes_bloat = {bloat_excludes}

datas = [
    ('pyproject.toml', '.'),                # CLI / Web 从源码配置读取版本，冻结后同样可用
    ('src/external_resources.json', 'src'),
    ('src/javascript', 'src/javascript'),   # JS 签名脚本（src/__init__.py 经 __file__ 定位 → _internal/src/javascript）
    *{i18n_datas},   # JSON 翻译目录（由 build_exe.i18n_datas_entries() 生成）
    ('web', 'web'),                         # Web 面板静态资源（src/api/routes.py 经 __file__ 定位 → _internal/web）
]
# 注意：config/ 不在此处（不进 _internal），由 copy_external_binaries 复制到 exe 同级，
# 以便程序在运行时直接读写配置。ffmpeg/ node/ 同理。
# excludes 里的 'customtkinter' / 'pystray' / 'PIL' / 'fastapi' 均为防御口径（依赖已摘除，但构建
# venv 仍有残留），防止残留引用把包重新拖回 _internal。
#
hidden_common = [
    'src.support.i18n',                          # main.py 内部延迟导入
    'h2',                            # httpx[http2] 懒加载依赖
    'exejs',                         # PyExecJS 继任者，try/except 条件导入需显式收集
]
# uvicorn 的协议/事件循环模块均为运行时按字符串导入，必须全量收集
hidden_web = hidden_common + collect_submodules('uvicorn')

# 注意：PyInstaller 6.x 已移除 cipher / zipped_data / zipfiles，spec 语法为 v6 风格。
# excludes 一律以 excludes_bloat 打头：两个入口共用同一个 _internal（COLLECT 合并去重），只在
# 某一个 Analysis 上排除没有意义——只要 Web 收了它，CLI 的产物里照样带着。
a_cli = Analysis(['src/main.py'], pathex=[SPECPATH], datas=datas, hiddenimports=hidden_common,
                 excludes=excludes_bloat + ['tkinter', 'customtkinter', 'pystray', 'PIL',
                           'fastapi', 'uvicorn', 'starlette', 'brotlicffi', 'PySide6'],
                 noarchive=False)
a_web = Analysis(['src/web.py'], pathex=[SPECPATH], datas=[], hiddenimports=hidden_web,
                 excludes=excludes_bloat + ['tkinter', 'customtkinter', 'pystray', 'PIL', 'PySide6', 'brotlicffi'],
                 noarchive=False)

pyz_cli = PYZ(a_cli.pure)
pyz_web = PYZ(a_web.pure)

exe_cli = EXE(pyz_cli, a_cli.scripts, [], exclude_binaries=True,
              name='{app}', console=True, contents_directory='_internal')
exe_web = EXE(pyz_web, a_web.scripts, [], exclude_binaries=True,
              name='{app}-Web', console=True, contents_directory='_internal')

coll = COLLECT(
    exe_cli, a_cli.binaries, a_cli.datas,
    exe_web, a_web.binaries, a_web.datas,
    strip=False, upx=False, name='{app}',
)
"""


# 解析 pyproject.toml 中的 version 字段（版本唯一事实源），返回版本字符串；解析失败回退 "0.0.0"
def read_version() -> str:
    text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*["\'](.+?)["\']', text, re.M)
    return m.group(1) if m else "0.0.0"


# 打包前预编译所有入口脚本与 src/ 源码，提前以文件名+行号暴露语法错误
def preflight_syntax_check() -> None:
    # 曾出现源码首行编码注释被破坏（丢失行首 '#'）导致三平台 CI 全部在 PyInstaller
    # Analysis 阶段报 IndentationError，且报错混在长日志里不易定位；提前编译可在几秒内以明确的
    # 文件名+行号失败。
    import py_compile

    targets = sorted((PROJECT_ROOT / "src").rglob("*.py"))
    for path in targets:
        if not path.is_file():
            continue
        try:
            _ = py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as exc:
            print(f"[build] 预检失败：{path.relative_to(PROJECT_ROOT)} 存在语法错误：\n{exc.msg}")
            sys.exit(1)
        # 额外守护：首行以空白开头即文件头被破坏（py_compile 已能捕获丢 '#' 的情形，
        # 这里再堵 BOM 以外的不可见前缀，防编辑器/网页端粘贴引入脏字节）。
        head = path.read_bytes()[:16]
        if head[:1] in (b" ", b"\t"):
            print(f"[build] 预检失败：{path.relative_to(PROJECT_ROOT)} 首行以空白开头（文件头可能被破坏）")
            sys.exit(1)
    print(f"[build] 预检通过：{len(targets)} 个源码文件语法正常")


# 生成 spec 模板并调用 PyInstaller 完成 CLI/Web 两入口 onedir 打包
def run_pyinstaller() -> None:
    preflight_syntax_check()
    _ = SPEC_PATH.write_text(
        SPEC_TEMPLATE.format(app=APP_NAME, bloat_excludes=list(BLOAT_EXCLUDES), i18n_datas=i18n_datas_entries()),
        encoding="utf-8",
    )
    print(f"[build] 已生成 spec：{SPEC_PATH}")
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(SPEC_PATH)]
    print(f"[build] PyInstaller 命令：\n  {' '.join(cmd)}")
    _ = subprocess.run(cmd, check=True, cwd=PROJECT_ROOT)


# 将仓库内 ffmpeg/node/config 复制到发布根目录（exe 同级），运行时可直接读写
def copy_external_binaries(include_runtime: bool = True) -> None:
    # 三者是运行时资源，必须与最终 exe 同级（而非收进 _internal/），程序才能直接读写配置、
    # 调用 FFmpeg / Node.js。仓库内缺失时不报错：运行时会下载对应平台的 ffmpeg / node；
    # include_runtime=False（--no-runtime）时跳过 ffmpeg/node，交用户首跑自动下载。
    # 若仓库内的 ffmpeg/node 是 Windows 二进制而当前平台非 Windows，同样跳过——否则产物里会
    # 带上一份跑不动的 exe。
    if include_runtime:
        for name in ("ffmpeg", "node"):
            src = PROJECT_ROOT / name
            if not src.is_dir():
                continue
            has_exe = any(p.suffix == ".exe" for p in src.iterdir())
            if has_exe and not IS_WIN:
                print(f"[build] 跳过 {name}/（Windows 二进制，与当前平台不符）")
                continue
            dst = RELEASE_DIR / name
            _ = shutil.copytree(src, dst, dirs_exist_ok=True)
            print(f"[build] 已复制 {name}/ -> {dst}")
    else:
        print("[build] --no-runtime：跳过 ffmpeg/ 与 node/（用户首次运行时自动下载）")

    # SEV-2213：**绝不能整目录原样复制**。仓库根的 config/config.ini 与 config/URL_config.ini
    # 含真实凭据（平台 Cookie / 代理账密 / Web 口令 / 内嵌 query token 的房间地址），而 make_zip
    # 会把发布目录整棵树压成对外分发的 zip——继 .gitignore 与 .dockerignore 之后的第三条外发通道。
    # 处置：按「模板化」逐个写入（结构保留、凭据置空），并由 make_zip 内的凭据断言兜第二道：
    # 这里是「不产生」，那里是「产生了就拦住」，同源判据但互不替代。
    _prepare_config_dir()


# 发布目录内**绝不允许**存在的凭据文件（SEV-2213）。与 .dockerignore 的 `config/config.ini` /
# `config/URL_config.ini` 两条排除项同源——同一批文件、同一份敏感判定，只是作用在「外发 zip」通道上。
CREDENTIAL_FILE_NAMES: tuple[str, ...] = ("config.ini", "URL_config.ini")


def _sanitized_config_text(text: str, *, fallback_header: str = "") -> str:
    # 把 config.ini 文本按「敏感键置空」重写为可分发模板，保留注释与节结构（产物仍是用户
    # 可直接编辑的模板，而非空文件）。
    # 敏感判定复用 src/web_config（is_sensitive_item + _looks_like_secret_value），不另写一份
    # 正则：那会造出第二份「什么算敏感」的事实源，两侧迟早漂移。
    # 用 configparser 而非逐行正则：ini 的 section/key 认法（`=` 与 `:` 均可、键名大小写不敏感）
    # 与运行时读取完全一致，不会出现「面板认为敏感、打包器不认为」的缝。
    import configparser

    from src.api import config as web_config

    parser = configparser.ConfigParser(interpolation=None)
    # 保留键名原样大小写，避免写回时把键名改成小写形态。
    # 类型注记：ConfigParser 里 optionxform 是方法，实例赋值 str 会触发 mypy
    #   [method-assign]/[assignment]；这是 configparser 的既知惯用法（CPython 文档亦如此
    #   演示），故就地标注忽略，而不是绕开该赋值。
    parser.optionxform = str  # type: ignore[method-assign,assignment]
    try:
        parser.read_string(text)
    except configparser.Error:
        # 解析不了（损坏/异构 ini）：**不得**退回「原样复制」——那正是本次要堵的形态。
        # 返回只含表头的空模板，宁可让用户重新填，也不外发可能含凭据的原文。
        return fallback_header or "# config.ini 解析失败，已置为空白模板（原文件未分发）\n"

    lines: list[str] = []
    for section in parser.sections():
        lines.append(f"[{section}]")
        for key, value in parser.items(section):
            # 空值（未启用）本就不含凭据，原样保留；非空且命中敏感判定的一律置空。
            # 判定 = web_config.is_sensitive_item（节白名单 Cookie/账号密码/Authorization + 键名模式：令牌/密码/
            # 授权码/token/secret/passwd/password/api_key/推送接口链接/推送地址/代理地址）或
            # _looks_like_secret_value（兜住「键名正常但值里嵌 token」，与面板脱敏同口径）。
            if value.strip() and (
                web_config.is_sensitive_item(section, key) or web_config._looks_like_secret_value(value)
            ):
                lines.append(f"{key} = ")
            else:
                lines.append(f"{key} = {value}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _sanitized_url_config_text() -> str:
    # URL_config.ini 一律重写为**单行注释样例**：该文件通篇是房间地址，而地址的 query 里可能内嵌平台 token
    # （本仓在 _looks_like_secret_value 里已承认这一形态）。样例串与 _prepare_url_config() 逐字同源。
    return "#https://live.douyin.com/000000000000\n"


def _prepare_config_dir() -> None:
    # 把发布目录的 config/ 写成**脱敏模板**（SEV-2213 的「不产生」一侧）。
    # 只对 CREDENTIAL_FILE_NAMES 里的两个文件脱敏；其余文件**原样复制、不过滤**——它们不属已知凭据面，
    # 新增任何可能带凭据的配置文件名必须先登记进该元组，否则会直接进对外 zip。
    cfg_dst = RELEASE_DIR / "config"
    cfg_dst.mkdir(parents=True, exist_ok=True)
    cfg_src = PROJECT_ROOT / "config"

    known = set(CREDENTIAL_FILE_NAMES)
    # 1) 凭据文件：从仓库源读取后脱敏重写（源不存在时落空白模板，绝不跳过）
    ini_src = cfg_src / "config.ini"
    template = (
        _sanitized_config_text(ini_src.read_text(encoding="utf-8-sig", errors="replace"))
        if ini_src.is_file()
        else "[录制设置]\n"
    )
    _ = (cfg_dst / "config.ini").write_text(template, encoding="utf-8")
    _ = (cfg_dst / "URL_config.ini").write_text(_sanitized_url_config_text(), encoding="utf-8")
    print(f"[build] 已生成脱敏配置模板 config/config.ini + config/URL_config.ini -> {cfg_dst}")

    # 2) 其余非凭据文件照常复制（保留「模板之外的附属文件也能带过去」行为）
    if cfg_src.is_dir():
        for item in sorted(cfg_src.iterdir()):
            if item.name in known or not item.is_file():
                continue
            _ = shutil.copy2(item, cfg_dst / item.name)
            print(f"[build] 已复制 config/{item.name} -> {cfg_dst / item.name}")


def assert_no_credentials_in_release() -> None:
    # SEV-2213 的「产生了就拦住」一侧：make_zip **之前**校验发布目录那两份文件确实不含敏感键的真实值，
    # 命中即非零退出——与 .dockerignore 同源口径。zip 一旦生成就可能已被 copy/上传，事后再删已无意义；
    # 判据也不是「文件是否存在」（文件**必须**存在，用户要能在 exe 同级编辑），而是逐键比对源值。
    from src.api import config as web_config

    assert RELEASE_DIR.is_dir(), f"{RELEASE_DIR} 不存在，无从校验"
    cfg_dst = RELEASE_DIR / "config"

    problems: list[str] = []

    # ① URL_config.ini：必须是注释样例（_sanitized_url_config_text 的口径），不得含有效房间行
    url_cfg = cfg_dst / "URL_config.ini"
    if url_cfg.is_file():
        for n, line in enumerate(url_cfg.read_text(encoding="utf-8-sig", errors="replace").splitlines(), 1):
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                problems.append(f"config/URL_config.ini:{n} 含未注释的有效行（可能内嵌房间/token）：{stripped[:80]!r}")

    # ② config.ini：逐键比对**仓库源文件的真实值**——命中即说明脱敏漏了该键。用「源值出现在发布文件里」
    #    作判据而不是「键名敏感」：前者是实际泄漏的事实、后者只是推断，且这样也覆盖「键名不在敏感表里
    #    但值确实是凭据」的漏网情形。
    src_ini = PROJECT_ROOT / "config" / "config.ini"
    dst_ini = cfg_dst / "config.ini"
    if src_ini.is_file() and dst_ini.is_file():
        dst_text = dst_ini.read_text(encoding="utf-8-sig", errors="replace")
        src_parser = _read_ini_pairs(src_ini)
        for section, key, value in src_parser:
            v = value.strip()
            # 只拦「有实质内容且确实敏感」的值；空值/纯布尔运维参数不算凭据。
            if len(v) < 4:
                continue
            if not (web_config.is_sensitive_item(section, key) or web_config._looks_like_secret_value(v)):
                continue
            if v in dst_text:
                problems.append(f"config/config.ini 的 [{section}] {key} 真实值未被脱敏（长度 {len(v)}）")

    if problems:
        print("[build][FATAL] 发布目录内检出凭据（SEV-2213：外发 zip 不得包含配置凭据）：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        raise SystemExit(1)
    print("[build] 发布目录凭据校验通过：config/ 内无敏感键真实值")


def _read_ini_pairs(path: Path) -> list[tuple[str, str, str]]:
    # 读 ini 为 (节, 键, 值) 三元组列表；解析失败返回空（由 assert_no_credentials_in_release 的
    # 成品侧检查兜底，此函数自身不参与放行判定）。
    import configparser

    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # type: ignore[method-assign,assignment]  # 同上：保留键名原样大小写
    try:
        parser.read(path, encoding="utf-8-sig")
    except configparser.Error:
        return []
    return [(section, key, value) for section in parser.sections() for key, value in parser.items(section)]


def _find_runtime_binary(root: Path, names: tuple[str, ...]) -> Path | None:
    # 按名字**递归**找：各上游压缩包内部层级并不统一（Windows 平铺、node 的 tar.gz 带 bin/），
    # 只查根目录会把「布局不同」误判成「组件缺失」。0 字节不算命中——下载被截断时文件是在的。
    if not root.is_dir():
        return None
    for name in names:
        for hit in sorted(root.rglob(name)):
            if hit.is_file() and hit.stat().st_size > 0:
                return hit
    return None


VERSION_PROBE_ARGS: dict[str, str] = {"ffmpeg": "-version", "node": "--version"}


def _probe_runnable(binary: Path, component: str) -> str | None:
    # 跑一次版本探针：文件存在不等于能用，能抓到「在包里但跑不动」与「解包丢了可执行位（PermissionError）」
    # 两类。取值一律查 VERSION_PROBE_ARGS，不在本函数内写死：三平台共用 `-version` 曾让 full 包一支都
    # 出不来（node 判它非法选项），反向统一成 `--version` 同样错（ffmpeg 返回码 8）。输出按字节读、
    # 显式解码失败也不参与判定（AGENTS.md「探测子进程输出一律按字节比较」）。
    probe_arg = VERSION_PROBE_ARGS[component]
    try:
        proc = subprocess.run(
            [str(binary), probe_arg],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        # 需要 e 时必须加括号：PEP 758 的无括号写法不支持 as 绑定（AGENTS.md 已记此坑）
        return f"无法执行：{type(e).__name__}: {e}"
    if proc.returncode != 0:
        first = (proc.stderr or b"").splitlines()[:1]
        detail = first[0].decode("latin-1", errors="replace") if first else f"退出码 {proc.returncode}"
        return f"执行 `{probe_arg}` 失败：{detail}"
    return None


def verify_runtime_binaries(target_dir: Path) -> list[str]:
    # Verify the installed files as well as archive integrity before publishing.
    problems: list[str] = []
    ffmpeg_dir = target_dir / "ffmpeg"
    node_dir = target_dir / "node"

    ffmpeg_bin = _find_runtime_binary(ffmpeg_dir, ("ffmpeg.exe", "ffmpeg"))
    if ffmpeg_bin is None:
        problems.append(f"{ffmpeg_dir} 内找不到非空的 ffmpeg 可执行文件")
    else:
        why = _probe_runnable(ffmpeg_bin, "ffmpeg")
        if why:
            problems.append(f"ffmpeg ({ffmpeg_bin}) {why}")

    node_bin = _find_runtime_binary(node_dir, ("node.exe", "node"))
    if node_bin is None:
        problems.append(f"{node_dir} 内找不到非空的 node 可执行文件")
    else:
        why_node = _probe_runnable(node_bin, "node")
        if why_node:
            problems.append(f"node ({node_bin}) {why_node}")
    return problems


def download_runtime_binaries(target_dir: Path) -> None:
    print("[build] 下载配置中的 FFmpeg 和 Node.js...")
    try:
        for name in ("ffmpeg", "node"):
            install_runtime(name, target_dir)
    except Exception as error:
        raise SystemExit(f"[build][FATAL] 运行时下载失败：{error}") from error
    problems = verify_runtime_binaries(target_dir)
    if problems:
        raise SystemExit("[build][FATAL] full 包运行时不可用：\n  " + "\n  ".join(problems))


RUNTIME_ARTIFACT_DIR_NAMES: tuple[str, ...] = ("logs", "backup_config", "downloads")


def _clean_runtime_artifacts_from_release() -> list[str]:
    # 压缩前删除发布目录里的运行期产物，返回被删条目的相对路径（供日志打印）。
    # 为什么必须删：冒烟以 cwd=RELEASE_DIR 连跑 CLI 25s / Web 90s，src/support/logger.py 会在包根落下
    # streamget.log / PlayURL.log / web_console.log（含 runner 绝对路径、用户名与告警堆栈），「缺键补写」
    # 还会改写包内 config.ini；随包分发即信息外泄。主修法是「先压缩、后冒烟」（见 main()），本函数是
    # 第二道：清掉上一次构建或本地手工跑过残留在 dist/ 里的同类产物（PyInstaller 的 --clean 只清自己的
    # 缓存）。rmtree 用 ignore_errors=True：Windows 偶发句柄占用不应让打包整体失败，leftovers 由
    # _assert_zip_has_no_runtime_artifacts 在实物上兜住。
    removed: list[str] = []
    for name in RUNTIME_ARTIFACT_DIR_NAMES:
        victim = RELEASE_DIR / name
        if victim.is_dir():
            shutil.rmtree(victim, ignore_errors=True)
            removed.append(f"{name}/")
    # __pycache__ 可能出现在任意层级（源码复制、包内脚本被跑过），按目录名自顶向下剔除
    for root, dirs, _files in os.walk(RELEASE_DIR):
        if "__pycache__" in dirs:
            victim = Path(root) / "__pycache__"
            shutil.rmtree(victim, ignore_errors=True)
            dirs.remove("__pycache__")
            removed.append(f"{victim.relative_to(RELEASE_DIR)}/")
    return removed


def _zip_member_is_runtime_artifact(name: str) -> bool:
    # 判定 zip 成员名（`unzip -l` 看到的那一列）是否为不得分发的运行期产物。
    # 成员名一律以 `DouyinLiveRecorder/` 打头（_zip_release_dir 的写法，与旧 shutil.make_archive 同构），
    # 故先剥掉该前缀再按相对路径判定；zip 规范的分隔符恒为 '/'（不用 os.sep，否则 Windows 上未必判得出、
    # Linux CI 上又恰好能过——「本地绿 CI 绿但判据不同」）。
    parts = [p for p in name.replace("\\", "/").split("/") if p and p != "."]
    if parts and parts[0] == APP_NAME:
        parts = parts[1:]
    if not parts:
        return False
    return parts[0] in RUNTIME_ARTIFACT_DIR_NAMES or "__pycache__" in parts


def _assert_zip_has_no_runtime_artifacts(zip_path: Path) -> None:
    # 发布产物清单断言（MID-2254）：zip 内不得出现 logs/ backup_config/ downloads/ 或任意 __pycache__。
    # 清理之后还要在实物上再查一次：清理是 best-effort（ignore_errors），而真正分发出去的是 zip——以实物
    # 为准才自证。本条等价于把「人工 unzip -l 看一眼」固化成机器断言；删掉它即重新引入「日志随包外发」
    # 这条失效路径（SEV-2213 只判凭据真实值，对不含凭据却泄露环境信息的日志完全无感，两者互不替代）。
    with zipfile.ZipFile(zip_path) as zf:
        bad = sorted({name for name in zf.namelist() if _zip_member_is_runtime_artifact(name)})
    if bad:
        raise SystemExit(
            "[build][FATAL] 发布 zip 含运行期产物，已终止（这些内容会随包外发给用户）：\n  " + "\n  ".join(bad[:20])
        )


# 把发布目录压成 zip；成员名一律 `<APP_NAME>/<相对路径>`（与旧实现 shutil.make_archive(root_dir=DIST_DIR,
# base_dir=APP_NAME) 逐字同构——_assert_zip_has_no_runtime_artifacts 的判据就是按这个前缀剥的，改了前缀
# 等于让那条断言永远绿）。不用 make_archive 是因为它固定用默认压缩级别、无法指定 compresslevel：实测
# 同一发布目录 level=9 比默认再小 1.36%（lite 54.84MB → 54.09MB）。
def _zip_release_dir(zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(RELEASE_DIR.rglob("*")):
            if path.is_file():
                zf.write(path, f"{APP_NAME}/{path.relative_to(RELEASE_DIR).as_posix()}")


# 将发布目录压缩为带平台/架构标识的 zip（suffix 用于 --dual 区分 lite / full），返回路径
def make_zip(version: str, suffix: str = "") -> Path:
    # 压缩是**对外分发的最后一道口**，故两道检查都放在这里而不是调用方，使「--no-zip 跳过压缩」与
    # 「压缩即校验」两条路径都不可能出「绕开校验的 zip」：SEV-2213 的凭据校验（敏感键真实值即退出）管
    # 「不得外发真实值」，MID-2254 的清理 + 实物断言管「不得外发环境信息」；判据不同、互不替代。
    removed = _clean_runtime_artifacts_from_release()
    if removed:
        print(f"[build] 压缩前已剔除发布目录内的运行期产物：{', '.join(removed)}")
    assert_no_credentials_in_release()
    os_tag = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    arch = platform.machine().lower()
    # `.zip` 必须与其余段拼进同一个 f-string，**不得**改用 Path.with_suffix(".zip")：版本号本身带点
    # （如 4.3.0），with_suffix 把最后一个点之后的全部内容当旧扩展名整段丢弃——实测（Windows 架构段是
    # amd64）DouyinLiveRecorder-v4.3.0-windows-amd64-lite → DouyinLiveRecorder-v4.3.zip，于是 lite 与 full
    # 落到同一路径、full 静默覆盖 lite（平台/架构标识也一起没了）。2026-09-27 CI 实红即此形态：
    # build-release.yml 的 lite/full 校验与 dist/*-lite.zip 上传通配双双落空。
    zip_path = DIST_DIR / f"{APP_NAME}-v{version}-{os_tag}-{arch}{suffix}.zip"
    # version / suffix 一旦含路径分隔符，f-string 拼出的就不再是**单个文件名**而是子路径，
    # 成品会落到 DIST_DIR 之外（Windows 下 pathlib 把 `\` 当分隔符，实测 `make_zip("4.3.0\p1")`
    # 的目标是 dist/DouyinLiveRecorder-v4.3.0/p1-<os>-<arch>.zip）。读版本失败时会回退 "0.0.0"，
    # 静默写到别处比报错更难查，故按「父目录必须还是 dist/」这一结构性不变量拦住（同时覆盖 suffix）。
    if zip_path.parent != DIST_DIR:
        raise SystemExit(f"[build][FATAL] zip 产物名含路径分隔符，会写到 {DIST_DIR} 之外：{zip_path}")
    _zip_release_dir(zip_path)
    _assert_zip_has_no_runtime_artifacts(zip_path)
    print(f"[build] 压缩包已生成：{zip_path}（{zip_path.stat().st_size / 1024 / 1024:.1f} MB）")
    return zip_path


# 压缩前剔除 dist/ 里上一次构建残留的 zip，并把被删名字打进日志（与 _clean_runtime_artifacts_from_release
# 同口径：删了什么都要写出来，不留静默删除）
def _clean_stale_release_zips() -> list[str]:
    # 只按 `{APP_NAME}-v*.zip` 命中，不通配 dist/ 下的一切：用户自己放在 dist/ 里的其它压缩包不归本次构建
    # 管辖。为什么必须删（2026-09-27 复核发布链时列为未闭合项）：build-release.yml 的「Upload build
    # artifact (inspection only)」用 `path: dist/*.zip`，凡是复用工作区的 runner（自建 / 手动 `--dual`
    # 反复跑）都会把上一轮的 zip 连同本轮一起扫进附件——包括 with_suffix 截断缺陷时期那种名字里既无平台也
    # 无变体的 `DouyinLiveRecorder-v4.3.zip`。必须在**第一个** zip 之前调用：放进 make_zip 内会让第二次删掉
    # 第一次的产物。
    removed: list[str] = []
    for victim in sorted(DIST_DIR.glob(f"{APP_NAME}-v*.zip")):
        victim.unlink()
        removed.append(victim.name)
    return removed


def _drop_stale_release_zips() -> None:
    stale = _clean_stale_release_zips()
    if stale:
        print(f"[build] 压缩前已剔除 dist/ 内的陈旧 zip：{', '.join(stale)}")


def _assert_dual_zips_are_two_files(lite_zip: Path, full_zip: Path) -> None:
    # make_zip 只自证**单次调用**的名字结构；「两次调用算出同一个路径」这种病（2026-09-27 的 with_suffix
    # 截断即此形态）只有事后比对两个返回值才看得见——第二次以 zipfile "w" 打开同名文件是原地截断，不报错、
    # 退出码 0。判据与 CI 的 `Verify dist contains both lite & full zips` 同源，区别只是那条在 runner 上、
    # 这条在出包的那台机器上，本地跑 `--dual` 不必等 CI 就能看到红。
    if lite_zip == full_zip:
        raise SystemExit(f"[build][FATAL] --dual 的 lite 与 full 落到同一个文件，后者已覆盖前者：{lite_zip}")
    missing = [zip_path.name for zip_path in (lite_zip, full_zip) if not zip_path.is_file()]
    if missing:
        raise SystemExit(f"[build][FATAL] --dual 已声称出包，但下列产物不存在：{', '.join(missing)}")


# ==================== 冒烟测试 ====================

FATAL_MARKERS = ("Traceback (most recent call last)", "ModuleNotFoundError", "ImportError")


# 准备 URL 配置文件：若为空则写入一条注释示例 URL，避免 CLI 阻塞在 input()。
# 配置在 exe 同级目录（而非 _internal），故写到 RELEASE_DIR/config；样例串与
# _sanitized_url_config_text() 逐字同源，两处不得各自演化。
def _prepare_url_config() -> None:
    url_cfg = RELEASE_DIR / "config" / "URL_config.ini"
    url_cfg.parent.mkdir(parents=True, exist_ok=True)
    if not url_cfg.exists() or not url_cfg.read_text(encoding="utf-8-sig", errors="ignore").strip():
        _ = url_cfg.write_text("#https://live.douyin.com/000000000000\n", encoding="utf-8")


# 以独立进程组/会话启动子进程，便于冒烟结束一次性杀掉整棵进程树（含应用 spawn 的 ffmpeg
# 子进程），避免子进程孤儿化被 runner 清理时刷屏
def _launch(exe: Path, extra_env: "dict[str, str] | None" = None) -> subprocess.Popen[str]:
    # Unix 用 start_new_session=True（子进程成为新会话首领，PGID == 其 PID），Windows 用
    # creationflags=CREATE_NEW_PROCESS_GROUP（新进程组，配合 taskkill /T 递归终止）；两个参数均
    # 显式传递（非本平台的分支取默认 0/False），既满足类型检查又保持跨平台语义。
    # extra_env 合并到 os.environ 后注入子进程（不覆盖其余变量）；冒烟测试用它向 Web 面板传
    #   DOUYIN_WEB_ALLOW_INSECURE=1 以绕过「非回环地址需认证」护栏。
    start_new_session = not IS_WIN
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if IS_WIN else 0
    launch_env = os.environ.copy()
    if extra_env:
        launch_env.update(extra_env)
    return subprocess.Popen(
        [str(exe)],
        cwd=RELEASE_DIR,
        env=launch_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=start_new_session,
        creationflags=creationflags,
    )


# 杀掉整个进程树（应用本体及其 ffmpeg 子进程），跨平台处理 Windows/Unix。
def _kill_tree(proc: subprocess.Popen[str]) -> None:  # pylint: disable=not-callable,no-member
    # 仅杀父进程会留下孤儿化的 ffmpeg，最终被 runner 的 orphan-process 清理收尸，
    # 产生大量 "Terminate orphan process" 噪声；此处连根拔起避免之。
    pid = proc.pid
    if IS_WIN:
        # taskkill /T 递归终止进程树，/F 强制；进程已退出时忽略错误
        _ = subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    # Unix：kill 整个进程组（子进程已是新会话首领，PGID == PID）。
    # getpgid/killpg 为 POSIX-only，用 getattr 兼容类型检查与跨平台运行。
    getpgid = getattr(os, "getpgid", None)
    if getpgid is None:
        return
    try:
        pgid = getpgid(pid)  # type: ignore[misc]  # pylint: disable=not-callable
    except ProcessLookupError:
        return
    killpg = getattr(os, "killpg", None)
    if killpg is None:
        return
    try:
        killpg(pgid, getattr(signal, "SIGKILL", 9))  # type: ignore[misc]  # pylint: disable=not-callable
    except ProcessLookupError:
        pass


# 收尾：两种服务都必须存活到探测完成；任何提前退出均失败。
def _finish(proc: subprocess.Popen[str], name: str) -> None:
    still_running = proc.poll() is None
    if still_running:
        _kill_tree(proc)
    try:
        out = proc.communicate(timeout=10)[0] or ""
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"[smoke:{name}] 进程收尾超时") from exc
    tail = "\n".join(out.splitlines()[-20:])
    print(f"[smoke:{name}] 进程输出（末尾 20 行）：\n{tail}")
    # 仅当进程已自行退出时，才把 FATAL_MARKERS 视为真实崩溃信号：进程仍存活（still_running）时输出
    # 里的 Traceback 多为应用捕获并记录的异常（asyncio 对未处理任务异常的默认处理会打印完整
    # Traceback 但 loop 继续运行；API 偶发网络错误重试时也会记录堆栈）。真正的崩溃（导入失败 /
    # 未捕获异常）会致进程退出，此时下方 returncode 校验与本判定共同覆盖该情形。
    if not still_running and any(m in out for m in FATAL_MARKERS):
        raise RuntimeError(f"[smoke:{name}] 检测到导入错误 / 崩溃堆栈，冒烟测试失败")
    if not still_running:
        raise RuntimeError(f"[smoke:{name}] 进程提前退出，退出码 {proc.returncode}")
    print(f"[smoke:{name}] 通过 ✅")


# 冒烟：运行 CLI 数秒确认进入监控循环且无崩溃堆栈
def smoke_cli(timeout: int = 25) -> None:
    exe = RELEASE_DIR / f"{APP_NAME}{EXE_SUFFIX}"
    print(f"[smoke:cli] 启动 {exe}（最长 {timeout}s）...")
    proc = _launch(exe)
    start = time.time()
    while time.time() - start < timeout and proc.poll() is None:
        time.sleep(0.5)
    _finish(proc, "cli")


# 冒烟：启动 Web 后 HTTP 探活首页（IPv4+localhost），能返回即视为面板可用
def smoke_web(timeout: int = 90, port: int = 8000) -> None:
    # 探测地址同时覆盖 IPv4(127.0.0.1) 与主机名(localhost)：config 默认 web_host=0.0.0.0 时两者均可达；
    # 若 web_host 改为 localhost，macOS 会优先解析为 IPv6(::1)，仅探 IPv4 会误判失败。
    # timeout 取较大值：macOS arm64 冷启动加载 uvicorn/starlette/httpx 较重，40s 易超时（平台性能差异，
    # 非应用缺陷）。
    # 用 ProxyHandler({}) 构造「无代理」opener：macOS 上 urllib 默认读系统代理配置（SystemConfiguration），
    # CI runner 的 localhost 请求可能被路由到不存在的代理而挂起，即便服务已正常监听。
    # 安全护栏（web.py C1）：未启用 Web 认证时拒绝监听非回环地址（config 默认 web_host=0.0.0.0）。冒烟只做
    # 本地 HTTP 探活、从不真正暴露到局域网，故注入 DOUYIN_WEB_ALLOW_INSECURE=1 放行——这正是该环境变量的
    # 设计用途；同时保留「真实绑定 0.0.0.0」的验证路径，比把 web_host 改成 127.0.0.1 更能暴露回归。
    exe = RELEASE_DIR / f"{APP_NAME}-Web{EXE_SUFFIX}"
    hosts = ("127.0.0.1", "localhost")
    print(f"[smoke:web] 启动 {exe}，探活 http://127.0.0.1:{port}/（最长 {timeout}s）...")
    proc = _launch(exe, extra_env={"DOUYIN_WEB_ALLOW_INSECURE": "1"})
    ok = False
    start = time.time()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        while time.time() - start < timeout and proc.poll() is None:
            for host in hosts:
                try:
                    with cast(http.client.HTTPResponse, opener.open(f"http://{host}:{port}/", timeout=3)) as resp:
                        if resp.status == 200:
                            ok = True
                            break
                except Exception:
                    continue
            if ok:
                break
            time.sleep(1)
    finally:
        _finish(proc, "web")
    if not ok:
        raise RuntimeError(f"[smoke:web] {timeout}s 内 HTTP 探活失败（端口 {port} 被占用或启动过慢也会导致此错误）")
    print("[smoke:web] HTTP 探活成功 ✅")


# 依次运行 CLI/Web 冒烟测试，全部通过才算成功
def smoke_test() -> None:
    _prepare_url_config()
    smoke_cli()
    smoke_web()
    print("[smoke] 全部冒烟测试通过 ✅")


# 将 stdout/stderr 配置为 UTF-8，避免 Windows CI 下中文日志抛 UnicodeEncodeError
def _ensure_utf8_streams() -> None:
    # Windows CI 的 stdout/stderr 默认 cp1252，输出中文日志会抛 UnicodeEncodeError；重配置为 UTF-8，并让
    # 后续派生的 Python 子进程也用 UTF-8。
    # errors 必须显式给 "replace"：reconfigure 只传 encoding 时会把错误处理器**重置为 "strict"**。本函数在
    # pytest 进程内被调用时（tests/test_build_exe.py 三条用例经 main() 调用），sys.stdout/stderr 正是 pytest
    # fd 捕获的 EncodedFile（原 errors="replace"），裸调等于把框架资产就地改成 strict，此后任何非 UTF-8 字节
    # 落进捕获文件都会在收尾读回时抛 UnicodeDecodeError，表现为「pytest 整轮无结论 + 无关用例报 FAILED/ERROR」
    # （2026-09-23 故障根因，见 DIAGNOSIS_2026-09-23_pytest-teardown-crash.md）。
    os.environ["PYTHONUTF8"] = "1"
    for s in (sys.stdout, sys.stderr):
        reconfigure = getattr(s, "reconfigure", None)
        if callable(reconfigure):
            try:
                _ = reconfigure(encoding="utf-8", errors="replace")
            except ValueError, OSError:
                pass


# 解析命令行参数并驱动打包/可选冒烟/可选压缩的全流程
def main() -> None:
    _ensure_utf8_streams()
    parser = argparse.ArgumentParser(description=f"{APP_NAME} 打包脚本")
    _ = parser.add_argument("--smoke", action="store_true", help="打包后运行冒烟测试")
    _ = parser.add_argument("--no-zip", action="store_true", help="跳过 zip 压缩")
    _ = parser.add_argument("--no-runtime", action="store_true", help="跳过 ffmpeg/node 打包（用户运行时自动下载）")
    _ = parser.add_argument(
        "--dual", action="store_true", help="同时生成 lite（无运行时）与 full（下载并打包 ffmpeg+node）两个 zip"
    )
    args = parser.parse_args()
    smoke = cast(bool, args.smoke)
    no_zip = cast(bool, args.no_zip)
    no_runtime = cast(bool, args.no_runtime)
    dual = cast(bool, args.dual)
    # --dual 与 --no-zip 互斥（2026-09-27）：--dual 分支从不读 no_zip，旧形态是「显式写了 --no-zip 却被
    # 静默无视，照样压出 lite + full 两个大 zip」。矛盾参数组合必须 fail-fast，而不是让其中一个不起作用。
    if dual and no_zip:
        raise SystemExit("[build][FATAL] --dual 与 --no-zip 互斥（--dual 只产出 lite + full 两个 zip）")

    version = read_version()
    print(f"[build] 项目版本：v{version}，平台：{sys.platform}/{platform.machine()}")

    if dual:
        # --dual：PyInstaller 只跑一次，先产 lite 再产 full，避免重复打包。
        run_pyinstaller()
        # 顺序（MID-2254）：lite zip → 下载运行时 → full zip → 冒烟。冒烟必须排在**两个** zip 之后：它以
        # cwd=RELEASE_DIR 连跑 CLI/Web 共约 120s，期间 src/support/logger.py 往包根写日志、应用还会「缺键补写」
        # 改写包内 config.ini；旧顺序（lite → 冒烟 → full）让对外分发的 full 包带上 runner 绝对路径/用户名/
        # 告警堆栈。「先出包再冒烟」不削弱冒烟：跑的还是 dist/<APP_NAME>/ 那三个 exe 本体（与 zip 内容逐文件
        # 同源），只是不再把运行期写入的产物压进包里；make_zip 内的清理 + 实物断言只是第二道，顺序是第一道。
        # 1) lite 版本：仅 config，不含运行时
        _drop_stale_release_zips()
        copy_external_binaries(include_runtime=False)
        _prepare_url_config()  # 确保配置就绪（冒烟测试也需要）
        lite_zip = make_zip(version, suffix="-lite")
        # full 版本：下载并打包 ffmpeg + node
        download_runtime_binaries(RELEASE_DIR)
        full_zip = make_zip(version, suffix="-full")
        _assert_dual_zips_are_two_files(lite_zip, full_zip)
        if smoke:
            smoke_test()
        print(f"[build] 完成。发布目录：{RELEASE_DIR}（已生成 lite + full 两个 zip：{lite_zip.name}、{full_zip.name}）")
        return

    run_pyinstaller()
    copy_external_binaries(include_runtime=not no_runtime)

    # 同上（MID-2254）：先压缩、后冒烟，zip 内容永不含冒烟产物
    zip_path: Path | None = None
    if not no_zip:
        _drop_stale_release_zips()
        zip_path = make_zip(version)

    if smoke:
        smoke_test()

    print(f"[build] 完成。发布目录：{RELEASE_DIR}" + (f"（产物：{zip_path.name}）" if zip_path else ""))


if __name__ == "__main__":
    main()
