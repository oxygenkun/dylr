import os
import shlex
import signal
import subprocess
import sys
import time
from typing import cast
from urllib.parse import urlsplit

from loguru import logger

from src.notifications import msg_push
from src.notifications.msg_push import bark, dingtalk, ntfy, pushplus, send_email, tg_bot, xizhi
from src.recording import state
from src.recording.postprocess import get_startup_info
from src.support import i18n, utils


def get_bark_level(live_url: str, default_level: str, overrides: dict[str, str]) -> str:
    """按配置的推送渠道（微信/钉钉/邮箱/TG/BARK/NTFY/PUSHPLUS）分发直播状态消息。"""

    def room_key(url: str) -> tuple[str, str]:
        """URLs share a room key regardless of query strings and trailing slashes."""
        url = url.strip()
        parsed = urlsplit(url if "://" in url else "https://" + url)
        return parsed.netloc.lower(), parsed.path.rstrip("/")

    target = room_key(live_url)
    for room_url, level in overrides.items():
        if room_key(room_url) == target and level in {"active", "passive", "timeSensitive", "critical"}:
            return level
    return default_level


def build_push_title(record_name: str, push_time: str) -> str:
    """解析通知标题：自定义标题优先，未配置时兜底到带主播名的开播文案。"""
    configured = state.push_message_title.strip()
    if configured:
        return msg_push.render_push_template(configured, record_name, push_time)
    return msg_push.render_push_content("{ROOM_NAME} 正在直播中", record_name, push_time)


def push_message(record_name: str, live_url: str, content: str) -> None:
    msg_title = build_push_title(record_name, time.strftime("%Y-%m-%d %H:%M:%S"))
    push_functions = {
        "微信": lambda: xizhi(state.xizhi_api_url, msg_title, content),
        "钉钉": lambda: dingtalk(state.dingtalk_api_url, content, state.dingtalk_phone_num, state.dingtalk_is_atall),
        "邮箱": lambda: send_email(
            state.email_host,
            state.login_email,
            state.email_password,
            state.sender_email,
            state.sender_name,
            state.to_email,
            msg_title,
            content,
            state.smtp_port,
            state.open_smtp_ssl,
        ),
        "TG": lambda: tg_bot(state.tg_chat_id, state.tg_token, content),
        "BARK": lambda: bark(
            state.bark_msg_api,
            title=msg_title,
            content=content,
            level=get_bark_level(live_url, state.bark_msg_level, state.bark_level_overrides),
            sound=state.bark_msg_ring,
        ),
        "NTFY": lambda: ntfy(
            state.ntfy_api,
            title=msg_title,
            content=content,
            tags=state.ntfy_tags,
            action_url=live_url,
            email=state.ntfy_email,
        ),
        "PUSHPLUS": lambda: pushplus(state.pushplus_token, msg_title, content),
    }

    for platform, func in push_functions.items():
        if platform in state.live_status_push.upper():
            try:
                result = func()  # type: ignore[no-untyped-call]
                result_dict = cast(dict[str, list[str | int]], result)
                logger.info(
                    i18n.tr(
                        "提示信息：已经将[{record_name}]直播状态消息推送至你的{platform}, 成功{success_count}, 失败{error_count}",
                        record_name=record_name,
                        platform=platform,
                        success_count=len(result_dict["success"]),
                        error_count=len(result_dict["error"]),
                    )
                )
            except Exception as e:
                state.color_obj.print_colored(
                    i18n.tr("直播消息推送到{platform}失败: {e}", platform=platform, e=utils.mask_credentials(str(e))),
                    state.color_obj.RED,
                )


_SCRIPT_TIMEOUT_SECONDS = 300.0

_SCRIPT_REAP_TIMEOUT_SECONDS = 10.0

_TREE_KILL_TIMEOUT_SECONDS = 5.0

_KILL_TREE_VIA_TASKKILL = os.name == "nt"


def _partial_output_bytes(value: object) -> bytes:
    """把 TimeoutExpired 里「已经读到的部分输出」收口成 bytes：communicate() 超时抛的异常带 stdout/stderr。"""
    return value if isinstance(value, bytes) else b""


def _kill_posix_process_group(pid: int) -> None:
    """POSIX 进程组回收：脚本以 start_new_session=True 启动 ⇒ 自成会话，进程组 ID == 子进程 PID，。"""
    if sys.platform == "win32":
        return
    os.killpg(os.getpgid(pid), signal.SIGKILL)


def _kill_process_tree(process: subprocess.Popen[bytes], masked_command: str) -> None:
    """超时后回收**整棵进程树**：任何失败一律退化到只杀直接子进程，绝不向调用方抛出——run_script。"""
    try:
        if _KILL_TREE_VIA_TASKKILL:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                capture_output=True,
                timeout=_TREE_KILL_TIMEOUT_SECONDS,
                check=False,
            )
        else:
            _kill_posix_process_group(process.pid)
    except Exception as e:
        logger.warning(
            i18n.tr(
                "执行自定义脚本失败: {command} - {type_name}: {e}",
                command=masked_command,
                type_name=type(e).__name__,
                e=utils.mask_credentials(str(e)),
            )
        )
    finally:
        try:
            process.kill()
        except OSError:
            pass


def _spawn_script(args: list[str]) -> subprocess.Popen[bytes]:
    """把「已脱敏的命令原文」拆成 argv 并起脚本进程；返回 Popen[bytes]（管道按字节读，解码在调用侧。"""
    if _KILL_TREE_VIA_TASKKILL:
        return subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=get_startup_info(state.os_type)
        )
    return subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        startupinfo=get_startup_info(state.os_type),
    )


def run_script(command: str) -> None:
    """执行用户自定义的录后脚本命令（shlex 拆分，不用 shell），打印其 stdout/stderr。"""
    masked_command = utils.mask_credentials(command)
    try:
        args = shlex.split(command)
        process = _spawn_script(args)
        try:
            stdout, stderr = process.communicate(timeout=_SCRIPT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            _kill_process_tree(process, masked_command)
            try:
                stdout, stderr = process.communicate(timeout=_SCRIPT_REAP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as e:
                stdout, stderr = _partial_output_bytes(e.stdout), _partial_output_bytes(e.stderr)
                logger.warning(
                    i18n.tr(
                        "执行自定义脚本失败: {command} - {type_name}: {e}",
                        command=masked_command,
                        type_name=type(e).__name__,
                        e=utils.mask_credentials(str(e)),
                    )
                )
            logger.error(
                i18n.tr(
                    "执行自定义脚本超时（{_SCRIPT_TIMEOUT_SECONDS} 秒），已终止: {command}",
                    _SCRIPT_TIMEOUT_SECONDS=_SCRIPT_TIMEOUT_SECONDS,
                    command=masked_command,
                )
            )
        stdout_decoded = stdout.decode("utf-8", errors="replace")
        stderr_decoded = stderr.decode("utf-8", errors="replace")
        if stdout_decoded.strip():
            print(stdout_decoded)
        if stderr_decoded.strip():
            print(stderr_decoded)
    except PermissionError as e:
        logger.error(
            i18n.tr(
                "执行自定义脚本失败（无执行权限）: {command} - {type_name}: {e}",
                command=masked_command,
                type_name=type(e).__name__,
                e=utils.mask_credentials(str(e)),
            )
        )
        logger.error("脚本无执行权限!, 若是Linux环境, 请先执行:chmod +x your_script.sh 授予脚本可执行权限")
    except OSError as e:
        logger.error(
            i18n.tr(
                "执行自定义脚本失败: {command} - {type_name}: {e}",
                command=masked_command,
                type_name=type(e).__name__,
                e=utils.mask_credentials(str(e)),
            )
        )
        logger.error("Please add `#!/bin/bash` at the beginning of your bash script file.")
    except ValueError as e:
        logger.error(
            i18n.tr(
                "脚本命令解析失败: {command} - {type_name}: {e}",
                command=masked_command,
                type_name=type(e).__name__,
                e=utils.mask_credentials(str(e)),
            )
        )


def record_error(key: str | None = None) -> None:
    """线程安全记录一次错误：累计计数 error_count 加一，并向错误率窗口追加样本 1。"""
    with state.max_request_lock:
        state.error_count += 1
        state.error_window.append(1)
    scheduler = state.scheduler
    if scheduler is not None:
        scheduler.record_failure(key)


def record_success(key: str | None = None) -> None:
    """线程安全记录一次成功的检测周期：向错误率窗口追加样本 0。"""
    with state.max_request_lock:
        state.error_window.append(0)
    scheduler = state.scheduler
    if scheduler is not None:
        scheduler.record_success(key)


def adjust_max_request() -> None:
    """守护线程主体：委托给并发调度器的自适应调速循环（自适应全局容量 + per-key 熔断）；死循环不返回。"""
    while state.scheduler is None:
        time.sleep(1)
    state.scheduler.adjust_loop()


def clear_record_info(record_name: str, record_url: str) -> None:
    """清理 record_name 的录制状态；若 record_url 已被注释则从运行列表移除并把监控计数减一。"""
    with state.record_state_lock:
        state.recording.discard(record_name)
        state.recording_time_list.pop(record_name, None)
        if record_url in state.url_comments and record_url in state.running_list:
            state.running_list.remove(record_url)
            state.monitoring -= 1
            state.color_obj.print_colored(
                i18n.tr("[{record_name}]已经从录制列表中移除\n", record_name=record_name), state.color_obj.YELLOW
            )


def remove_room_from_running(record_url: str) -> None:
    """房间线程退出时从运行列表移除 record_url 并把监控计数减一（幂等：已被 clear_record_info 移除。"""
    if not record_url:
        return
    with state.record_state_lock:
        if record_url in state.running_list:
            state.running_list.remove(record_url)
            state.monitoring -= 1
