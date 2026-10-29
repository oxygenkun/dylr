import os
import signal
import subprocess
import threading
import time

from loguru import logger

from src.app.graceful_shutdown import shutdown_manager
from src.support import i18n

_ffmpeg_processes: list[subprocess.Popen[bytes]] = []
_processes_lock: threading.Lock = threading.Lock()

_TERMINATE_TIMEOUT_SECONDS = 30.0
_CLEANUP_MAX_WORKERS = 8
_CLEANUP_BUDGET_MARGIN_SECONDS = 5.0
_CLEANUP_WAIT_SECONDS: float | None = None
_MIN_STAGE_WAIT_SECONDS = 1.0

_QUIT_VIA_STDIN = os.name == "nt"


class _CleanupSignal(threading.Event):
    def __init__(self) -> None:
        super().__init__()
        self.terminated = False


def _cleanup_wait_budget(process_count: int, worker_count: int) -> float:
    """清理总等待预算（秒）：组内串行、组间并行 ⇒ 墙钟下界 = 最忙那一组的串行长度 × 单进程超时。"""
    serial_depth = -(-max(process_count, 0) // max(worker_count, 1))  # 向上取整
    budget = serial_depth * _TERMINATE_TIMEOUT_SECONDS + _CLEANUP_BUDGET_MARGIN_SECONDS
    cap = _CLEANUP_WAIT_SECONDS
    if cap is not None:
        return min(budget, float(cap))
    return budget


def register_ffmpeg_process(process: subprocess.Popen[bytes]) -> None:
    """把 process（新启动的 ffmpeg 子进程）登记到全局列表，供退出时统一清理；无返回值。"""
    with _processes_lock:
        _ffmpeg_processes.append(process)


def unregister_ffmpeg_process(process: subprocess.Popen[bytes]) -> None:
    """从全局列表中移除已结束的 process；列表中不存在时静默跳过，无返回值。"""
    shutdown_manager.forget(process)
    with _processes_lock:
        if process in _ffmpeg_processes:
            _ffmpeg_processes.remove(process)


def _stage_budget(deadline: float, stages_left: int) -> float:
    """按剩余预算给下一段等待分配秒数：三段均分「剩余时间」并保底 _MIN_STAGE_WAIT_SECONDS。"""
    remaining = deadline - time.monotonic()
    return max(_MIN_STAGE_WAIT_SECONDS, remaining / max(stages_left, 1))


def _terminate_ffmpeg_process(proc: subprocess.Popen[bytes], timeout: int = int(_TERMINATE_TIMEOUT_SECONDS)) -> bool:
    """分级终止 ffmpeg 子进程 proc（写 q / SIGINT → terminate → kill），timeout 为总等待秒数。"""
    if shutdown_manager.owns(proc):
        shutdown_manager.stop_process(proc)
        proc.wait()
        return True
    if proc.poll() is not None:
        return True
    deadline = time.monotonic() + max(float(timeout), _MIN_STAGE_WAIT_SECONDS * 3)
    try:
        if _QUIT_VIA_STDIN:
            if proc.stdin:
                try:
                    _ = proc.stdin.write(b"q")
                    proc.stdin.flush()
                except Exception:
                    pass
                finally:
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass
        else:
            try:
                proc.send_signal(signal.SIGINT)
            except Exception:
                pass

        try:
            _ = proc.wait(timeout=_stage_budget(deadline, 3))
            if proc.poll() is not None:
                return True
        except Exception:
            pass

        try:
            proc.terminate()
            _ = proc.wait(timeout=_stage_budget(deadline, 2))
            if proc.poll() is not None:
                return True
        except Exception:
            pass

        try:
            proc.kill()
            _ = proc.wait(timeout=_stage_budget(deadline, 1))
            if proc.poll() is not None:
                return True
        except Exception:
            pass

        try:
            if proc.stdout:
                proc.stdout.close()
        except Exception:
            pass

        return proc.poll() is not None
    except Exception as e:
        logger.error(i18n.tr("终止 ffmpeg 进程时出错: {e}", e=e))
        return False


def _cleanup_single_ffmpeg_process(
    proc: subprocess.Popen[bytes], finished: _CleanupSignal | threading.Event | None = None
) -> None:
    """清理单个 ffmpeg 进程并打印日志，供清理线程串行调用；异常内部吞掉，无返回值。"""
    try:
        if proc.poll() is None:
            logger.info(i18n.tr("尝试终止 ffmpeg 进程 (PID: {pid})", pid=proc.pid))
            if not _terminate_ffmpeg_process(proc):
                logger.warning(
                    i18n.tr(
                        "ffmpeg 进程 (PID: {pid}) 未能完全终止，请手动检查",
                        pid=proc.pid,
                    )
                )
                return
        logger.info(i18n.tr("ffmpeg 进程 (PID: {pid}) 已清理", pid=proc.pid))
        if isinstance(finished, _CleanupSignal):
            finished.terminated = True
    except Exception as e:
        logger.error(i18n.tr("清理 ffmpeg 进程时出错: {e}", e=e))
    finally:
        if finished is not None:
            finished.set()


def cleanup_all_ffmpeg_processes() -> None:
    """用守护线程分组并行清理全部已注册的 ffmpeg 进程并清空注册表；无入参，无返回值。"""
    logger.info("正在清理所有 ffmpeg 进程...")
    with _processes_lock:
        processes_to_clean = list(_ffmpeg_processes)

    tracked: list[tuple[subprocess.Popen[bytes], _CleanupSignal]] = []
    if processes_to_clean:
        worker_count = min(len(processes_to_clean), _CLEANUP_MAX_WORKERS)
        groups: list[list[tuple[subprocess.Popen[bytes], _CleanupSignal]]] = [[] for _ in range(worker_count)]
        for index, proc in enumerate(processes_to_clean):
            entry = (proc, _CleanupSignal())
            groups[index % worker_count].append(entry)
            tracked.append(entry)

        def _run_group(members: list[tuple[subprocess.Popen[bytes], _CleanupSignal]]) -> None:
            for member_proc, member_event in members:
                _cleanup_single_ffmpeg_process(member_proc, member_event)

        for group_index, group in enumerate(groups):
            if not group:
                continue
            thread = threading.Thread(
                target=_run_group, args=(group,), name=f"ffmpeg_cleanup_{group_index}", daemon=True
            )
            thread.start()

        budget = _cleanup_wait_budget(len(processes_to_clean), worker_count)
        deadline = time.monotonic() + budget
        pending_pids: list[str] = []
        for proc, finished in tracked:
            remaining = deadline - time.monotonic()
            if not finished.wait(timeout=max(0.0, remaining)):
                pending_pids.append(str(proc.pid))
        if pending_pids:
            logger.warning(
                i18n.tr(
                    "ffmpeg 进程清理未在 {seconds} 秒总预算内全部确认，已放弃等待（未确认 {count} 个: {pids}）",
                    seconds=f"{budget:.0f}",
                    count=len(pending_pids),
                    pids=",".join(pending_pids),
                )
            )

    with _processes_lock:
        confirmed_terminated = {id(proc) for proc, tracker in tracked if tracker.is_set() and tracker.terminated}
        still_running = [p for p in _ffmpeg_processes if id(p) not in confirmed_terminated]
        _ffmpeg_processes.clear()
        _ffmpeg_processes.extend(still_running)
    if still_running:
        confirmed_failed = sum(1 for _proc, tracker in tracked if tracker.is_set() and not tracker.terminated)
        unconfirmed = max(0, len(still_running) - confirmed_failed)
        logger.warning(
            i18n.tr(
                "{count} 个 ffmpeg 进程已保留在注册表中待下次清理（确认未能终止 {failed_count} 个、未确认 {unconfirmed_count} 个）",
                count=len(still_running),
                failed_count=confirmed_failed,
                unconfirmed_count=unconfirmed,
            )
        )
    logger.info("所有 ffmpeg 进程清理完成")


def _get_error_line(e: BaseException) -> str:
    """从异常 e 的 traceback 最内层取真正出错的行号；返回行号字符串（无 traceback 时返回 "unknown"）。"""
    tb = e.__traceback__
    if not tb:
        return "unknown"
    while tb.tb_next is not None:
        tb = tb.tb_next
    return str(tb.tb_lineno)
