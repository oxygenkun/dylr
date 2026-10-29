import asyncio
import ctypes
import os
import sys
import threading
from datetime import datetime
from typing import cast

from src.support import i18n
from src.support.console import configure_encoding
from src.support.logger import logger

_KERNEL32: ctypes.CDLL | None = None
_USER32: ctypes.CDLL | None = None


def _get_kernel32() -> ctypes.CDLL | None:
    """加载 kernel32（WinDLL）并声明参数/返回类型；失败返回 None。"""
    global _KERNEL32
    if _KERNEL32 is not None:
        return _KERNEL32
    if sys.platform != "win32":
        return None
    try:
        dll = ctypes.WinDLL("kernel32", use_last_error=True)
        dll.GetConsoleWindow.restype = ctypes.c_void_p
        _KERNEL32 = dll
        return dll
    except Exception:
        return None


def _get_user32() -> ctypes.CDLL | None:
    """加载 user32（WinDLL）；失败返回 None。"""
    global _USER32
    if _USER32 is not None:
        return _USER32
    if sys.platform != "win32":
        return None
    try:
        dll = ctypes.WinDLL("user32", use_last_error=True)
        dll.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        dll.ShowWindow.restype = ctypes.c_int
        _USER32 = dll
        return dll
    except Exception:
        return None


configure_encoding()


_WEB_CONSOLE_MAX_BYTES = 2 * 1024 * 1024
_WEB_CONSOLE_KEEP_BYTES = 512 * 1024


def _cap_web_console_log(log_path: str) -> int:
    """超限则把 web_console.log 就地截断为末尾若干 KB，返回丢弃字节数（0 = 无需处理）。"""
    try:
        size = os.path.getsize(log_path)
    except OSError:
        return 0
    if size <= _WEB_CONSOLE_MAX_BYTES:
        return 0
    try:
        with open(log_path, "r+b") as handle:
            _ = handle.seek(max(0, size - _WEB_CONSOLE_KEEP_BYTES))
            tail = handle.read()
            newline_at = tail.find(b"\n")
            if newline_at >= 0:
                tail = tail[newline_at + 1 :]
            else:
                tail = b""
            handle.seek(0)
            handle.truncate()
            _ = handle.write(tail)
        return size - len(tail)
    except OSError as e:
        print(i18n.tr("[web] web_console.log 超限截断失败: {type_name}: {e}", type_name=type(e).__name__, e=e))
        return 0


def _enter_background_mode(logs_dir: str, host: str, port: int) -> None:
    """进入后台模式：stdout/stderr 重定向到 logs/web_console.log；Windows 下另隐藏控制台窗口。"""
    log_path = os.path.join(logs_dir, "web_console.log")

    print("[web] 进入后台运行模式，控制台窗口将隐藏")
    print(i18n.tr("[web] 日志文件: {log_path}", log_path=log_path))
    print(i18n.tr("[web] 访问地址: http://{host}:{port}", host=host, port=port))
    _flush = getattr(sys.stdout, "flush", None)
    if callable(_flush):
        _ = _flush()

    _dropped = _cap_web_console_log(log_path)
    if _dropped > 0:
        print(
            i18n.tr(
                "[web] web_console.log 已超过 {max_mb} MB 上限，丢弃最早的 {dropped_kb} KB",
                max_mb=_WEB_CONSOLE_MAX_BYTES // (1024 * 1024),
                dropped_kb=_dropped // 1024,
            )
        )

    log_stream = open(log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = log_stream
    sys.stderr = log_stream
    try:
        from src.support.logger import rebind_console_sink

        rebind_console_sink()
    except Exception:
        pass

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'=' * 60}")
    print(i18n.tr("[{ts}] Web 管理面板进入后台运行模式", ts=ts))
    print(i18n.tr("控制台窗口已隐藏，访问地址: http://{host}:{port}", host=host, port=port))
    print(i18n.tr("日志文件: {log_path}", log_path=log_path))
    print("如需恢复控制台显示，请在 config.ini 设置 web_show_console = true 后重启")
    print(f"{'=' * 60}\n")

    if sys.platform == "win32":
        try:
            _k32 = _get_kernel32()
            _user32 = _get_user32()
            if _k32 is not None and _user32 is not None:
                hwnd = _k32.GetConsoleWindow()
                if hwnd:
                    _user32.ShowWindow(hwnd, 0)
        except Exception:
            pass


def _is_loopback_host(host: str) -> bool:
    """判断 host 是否为回环地址（仅本机可访问）。"""
    from src.api.config import is_loopback_bind_host

    return is_loopback_bind_host(host)


def main() -> None:
    """Web 管理面板入口：启动录制引擎守护线程与 uvicorn HTTP 服务。"""
    import uvicorn

    from src.api.config import read_web_config
    from src.api.routes import create_app
    from src.recording import state

    config_file = state.config_file
    url_config_file = state.url_config_file
    downloads_root = state.default_path
    logs_dir = os.path.join(state.script_path, "logs")
    os.makedirs(logs_dir, exist_ok=True)

    web_cfg = read_web_config(config_file)
    host: str = cast(str, web_cfg["web_host"])
    port: int = cast(int, web_cfg["web_port"])

    if not web_cfg["web_auth_enable"] and not _is_loopback_host(host):
        allow_insecure = os.environ.get("DOUYIN_WEB_ALLOW_INSECURE", "").strip().lower() in ("1", "true", "yes")
        if not allow_insecure:
            _reject_msg = i18n.tr(
                "[web] ❌ 拒绝启动: 未启用 Web 认证时不允许监听非回环地址 ({host})。请二选一:", host=host
            )
            print(_reject_msg)
            print("      1. config.ini [Web] 节设置 web_auth_enable = true 并配置 web_password；")
            print("      2. 或设置 web_host = 127.0.0.1 仅限本机访问。")
            print("      如确需在无认证状态暴露到局域网，请设置环境变量 DOUYIN_WEB_ALLOW_INSECURE=1 后重启（不推荐）。")
            try:
                logger.warning(_reject_msg)
            except Exception:
                pass
            sys.exit(1)
        _insecure_msg = i18n.tr(
            "[web] ⚠️ 严重安全警告: Web 面板正以「无认证 + 非回环地址」运行，能访问该地址的任何人可执行：\n"
            "① 读写全部配置（仅「自定义脚本执行命令」被禁止）；② 读取 logs 下的运行日志；"
            "③ 遍历并下载 downloads 下全部录制文件；④ 增删改直播间并可提交任意地址"
            "（存在被用作内网探测跳板的风险）；⑤ 启停录制。\n"
            "建议立即在 config.ini [Web] 节设置 web_auth_enable = true 并配置 web_password，"
            "然后删除环境变量 DOUYIN_WEB_ALLOW_INSECURE 并重启。\n"
            "另请注意（M-10）：绑定 0.0.0.0/:: 的远程部署下，写操作另受 Origin 同源校验约束——"
            "未在 config.ini 的 web_allowed_hosts 登记访问用主机名/IP 时，面板会「读得到、写不了」；"
            "该登记须手工编辑 config.ini 完成。"
        )
        print(_insecure_msg)
        try:
            logger.warning(_insecure_msg)
        except Exception:
            pass

    if not web_cfg["web_show_console"]:
        _enter_background_mode(logs_dir, host, port)

    _auto_start = os.environ.get("DOUYIN_WEB_AUTO_START_RECORDING", "").strip().lower() in ("1", "true", "yes")
    state.recording_enabled = _auto_start
    from src.recording import engine as recorder

    recorder.initialize_process()
    recorder_thread = threading.Thread(
        target=recorder.main,
        name="recorder-engine",
        daemon=True,
        kwargs={"non_interactive": True},
    )
    recorder_thread.start()
    state._recorder_thread = recorder_thread  # 供 get_status() 检测存活（I6）
    print(i18n.tr("[web] 录制引擎已在守护线程启动 (tid={ident})", ident=recorder_thread.ident))

    app = create_app(
        config_file=config_file,
        url_config_file=url_config_file,
        downloads_root=downloads_root,
        logs_dir=logs_dir,
        bind_host=host,
        bind_port=port,
    )

    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info", proxy_headers=False))

    print(i18n.tr("[web] Web 管理面板启动中: http://{host}:{port}", host=host, port=port))
    _auth_text = i18n.tr("已启用") if web_cfg["web_auth_enable"] else i18n.tr("未启用")
    print(i18n.tr("[web] 认证: {web_auth_enable}", web_auth_enable=_auth_text))

    try:
        asyncio.run(server.serve())
    finally:
        try:
            recorder._shutdown_postprocess_executor()
        except Exception as e:
            print(i18n.tr("后处理线程池收尾失败: {type_name}: {e}", type_name=type(e).__name__, e=e))
        try:
            recorder.cleanup_all_ffmpeg_processes()
        except Exception as e:
            print(i18n.tr("[web] 清理 ffmpeg 进程失败: {type_name}: {e}", type_name=type(e).__name__, e=e))
        try:
            from src.network.http import close_clients

            close_clients()
        except Exception as e:
            print(i18n.tr("[web] 清理 HTTP 连接池失败: {type_name}: {e}", type_name=type(e).__name__, e=e))


if __name__ == "__main__":
    main()
