from __future__ import annotations

import asyncio
import queue
import threading
import time
from typing import Any, Optional, Type, cast

from src.danmaku.base import DanmakuBase, DanmakuMessage, DanmakuMessageType, spawn_danmaku_task
from src.danmaku.monitor import DanmakuMonitorHub
from src.danmaku.srt_writer import SrtWriter
from src.support import i18n, utils
from src.support.logger import logger

_SRT_QUEUE_MAXSIZE = 10000
_SRT_WARN_INTERVAL = 60.0
_DANMAKU_TEXT_LIMIT = 2000

_SHUTDOWN_TIMEOUT_SECONDS = 5.0

_LOOP_RUNNING_WAIT_SECONDS = 0.5
_LOOP_RUNNING_POLL_INTERVAL = 0.02
_SRT_WRITER_JOIN_SECONDS = 3.0
_SRT_QUEUE_POLL_SECONDS = 0.5
_SRT_SHUTDOWN_QUEUE_FULL = "queue-full"
_SRT_SHUTDOWN_JOIN_TIMEOUT = "join-timeout"


class DanmakuCollector:
    def __init__(
        self,
        danmaku_cls: Type[DanmakuBase],
        danmaku_args: Any,
        base_filename: str,
        segment_seconds: Optional[float] = 1800.0,
        only_fans: Optional[bool] = None,
        room_name: Optional[str] = None,
        platform_name: Optional[str] = None,
        write_srt: bool = True,
        monitor: Optional[DanmakuMonitorHub] = None,
    ) -> None:
        """仅记非显然语义：write_srt=False 表示只监控、不落 SRT 文件（弹幕监控开、录制关）；monitor 可。"""
        self._danmaku_cls = danmaku_cls
        self._danmaku_args = danmaku_args
        self._srt: Optional[SrtWriter] = (
            SrtWriter(base_filename=base_filename, segment_seconds=segment_seconds) if write_srt else None
        )
        self._only_fans = only_fans
        _cls_name = str(getattr(self._danmaku_cls, "__name__", "danmaku"))
        self._room_name = room_name or _cls_name
        self._platform_name = platform_name or _cls_name
        self._cls_name = _cls_name
        self._monitor: Optional[DanmakuMonitorHub] = monitor

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._danmaku: Optional[DanmakuBase] = None
        self._stop_event = threading.Event()
        self._started = False
        self._stop_called = False  # stop() 防重入标记：提前中断与尾收兜底可能重复触发
        self._msg_count = 0
        self._start_task: "Optional[asyncio.Task[None]]" = None
        self._shutdown_task: "Optional[asyncio.Task[None]]" = None
        self._srt_queue: "queue.Queue[Optional[tuple[str, str, float]]]" = queue.Queue(maxsize=_SRT_QUEUE_MAXSIZE)
        self._srt_dropped = 0  # 因队列满被丢弃的弹幕条数（丢弃必须可观测，见下）
        self._srt_writer: Optional[threading.Thread] = None
        self._srt_stop = threading.Event()

    def _monitor_hub(self) -> Optional[DanmakuMonitorHub]:
        """惰性解析监控枢纽：未显式注入时取进程级单例（失败静默返回 None，监控缺位不影响录制）。"""
        if self._monitor is None:
            try:
                from src.danmaku.monitor import get_hub

                self._monitor = get_hub()
            except Exception:
                return None
        return self._monitor

    def start(self) -> None:
        """启动采集：锚定 SRT 时间轴并拉起后台守护线程，重复调用无效；不抛异常（失败只记日志）。"""
        if self._started:
            return
        self._started = True
        if self._srt is not None:
            try:
                self._srt.start()
            except Exception as e:
                logger.warning(i18n.tr("[弹幕采集]SRT 初始化失败(继续尝试写弹幕): {e}", e=e))
            try:
                writer = threading.Thread(
                    target=self._srt_writer_loop, name=f"srt_writer_{self._cls_name}", daemon=True
                )
                writer.start()
                self._srt_writer = writer
            except Exception as e:
                logger.warning(
                    i18n.tr(
                        "[弹幕采集]{cls_name} SRT 写线程启动失败,放弃本次弹幕采集: {type_name}: {e}",
                        cls_name=self._cls_name,
                        type_name=type(e).__name__,
                        e=e,
                    )
                )
                self._rollback_start(room_registered=False)
                return
        hub = self._monitor_hub()
        if hub is not None:
            hub.room_started(self._room_name, self._platform_name)
        try:
            worker = threading.Thread(target=self._run, name=f"danmaku_{self._cls_name}", daemon=True)
            worker.start()
            self._thread = worker
        except Exception as e:
            logger.warning(
                i18n.tr(
                    "[弹幕采集]{cls_name} 采集线程启动失败,放弃本次弹幕采集: {type_name}: {e}",
                    cls_name=self._cls_name,
                    type_name=type(e).__name__,
                    e=e,
                )
            )
            self._rollback_start(room_registered=True, close_reason="采集线程启动失败")
            return

    def _rollback_start(self, room_registered: bool, close_reason: str = "") -> None:
        """start() 的回滚出口（MID-2242）：「不抛异常」不等于「可以留在半启动状态」——_started 若仍为。"""
        writer = self._srt_writer
        if writer is not None:
            try:
                self._srt_queue.put_nowait(None)
            except queue.Full:
                self._srt_stop.set()
            self._join_thread(writer, _SRT_WRITER_JOIN_SECONDS)
        self._srt_writer = None
        self._thread = None
        self._started = False
        if room_registered:
            hub = self._monitor_hub()
            if hub is not None:
                hub.room_closed(self._room_name, close_reason)

    @staticmethod
    def _join_thread(thread: threading.Thread, timeout: float) -> None:
        """join 一个「从未成功 start()」的 Thread 会先抛 RuntimeError: cannot join thread before it is。"""
        if thread.ident is None:
            return
        try:
            thread.join(timeout=timeout)
        except RuntimeError as e:
            logger.debug(
                i18n.tr(
                    "[弹幕采集]{where} 异常(忽略): {type_name}: {e}",
                    where="thread.join",
                    type_name=type(e).__name__,
                    e=e,
                )
            )

    def stop(self, timeout: float = 8.0) -> None:
        """停止采集：置停止标记、跨线程调度关闭弹幕连接，最多等待 timeout 秒回收线程，最后关闭 SRT 文件。"""
        if self._stop_called:
            return
        self._stop_called = True
        hub = self._monitor_hub()
        if hub is not None:
            hub.room_closed(self._room_name, i18n.tr("采集停止"))
        if not self._started:
            if self._srt is not None:
                self._srt.close()
            return
        self._stop_event.set()
        loop = self._loop
        if loop is not None:
            deadline = time.monotonic() + _LOOP_RUNNING_WAIT_SECONDS
            while not loop.is_running() and not loop.is_closed():
                if time.monotonic() >= deadline:
                    break
                time.sleep(_LOOP_RUNNING_POLL_INTERVAL)
            if loop.is_running():
                try:
                    loop.call_soon_threadsafe(self._schedule_stop)
                except Exception as e:
                    logger.debug(
                        i18n.tr(
                            "[弹幕采集]{where} 异常(忽略): {type_name}: {e}",
                            where="call_soon_threadsafe",
                            type_name=type(e).__name__,
                            e=e,
                        )
                    )
        if self._thread is not None:
            self._join_thread(self._thread, timeout)
        if self._srt_writer is not None and self._srt is not None:
            reason: Optional[str] = None
            try:
                self._srt_queue.put_nowait(None)
            except queue.Full:
                self._srt_stop.set()
                reason = _SRT_SHUTDOWN_QUEUE_FULL
            self._join_thread(self._srt_writer, _SRT_WRITER_JOIN_SECONDS)
            if reason is None and self._srt_writer.is_alive():
                reason = _SRT_SHUTDOWN_JOIN_TIMEOUT
            if reason is not None:
                logger.warning(
                    i18n.tr(
                        "[弹幕采集]SRT 写线程收尾未完成（{reason}），尾部弹幕可能未落盘",
                        reason=reason,
                    )
                )
        if self._srt is not None:
            self._srt.close()

    def _schedule_stop(self) -> None:
        """在采集线程的 loop 中执行：投递一个关闭协程用于停止弹幕客户端并停掉 loop，无返回值。"""

        async def _shutdown() -> None:
            """内部协程：先 await 弹幕客户端 stop()（异常记日志），再取消 danmaku.start() 任务、。"""
            if self._danmaku is not None:
                try:
                    await asyncio.wait_for(self._danmaku.stop(), timeout=_SHUTDOWN_TIMEOUT_SECONDS)
                except TimeoutError:
                    logger.warning(
                        i18n.tr("[弹幕采集]{cls_name} stop() 超时,强制停止事件循环", cls_name=self._cls_name)
                    )
                except Exception as e:
                    logger.debug(
                        i18n.tr(
                            "[弹幕采集]{where} 异常(忽略): {type_name}: {e}",
                            where="danmaku.stop",
                            type_name=type(e).__name__,
                            e=e,
                        )
                    )
            task = self._start_task
            if task is not None and not task.done():
                task.cancel()
                await asyncio.sleep(0)
            if self._loop is not None:
                self._loop.stop()

        if self._loop is not None:
            self._shutdown_task = spawn_danmaku_task(_shutdown())

    def _run(self) -> None:
        """采集线程主体：新建并绑定事件循环，实例化平台弹幕类（注入三个回调、透传 only_fans），。"""
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        if self._stop_event.is_set():
            logger.debug(
                i18n.tr(
                    "[弹幕采集]{cls_name} 启动前已收到停止信号,跳过连接",
                    cls_name=self._cls_name,
                )
            )
            try:
                loop.close()
            except Exception:
                pass
            self._loop = None
            return
        try:
            danmaku = self._danmaku_cls(
                on_message=self._on_message,
                on_close=self._on_close,
                on_ready=self._on_ready,
            )
            if self._only_fans is not None and hasattr(danmaku, "_only_fans"):
                cast(Any, danmaku)._only_fans = self._only_fans
            self._danmaku = danmaku
            if self._stop_event.is_set():
                logger.debug(
                    i18n.tr(
                        "[弹幕采集]{cls_name} 启动前已收到停止信号,跳过连接",
                        cls_name=self._cls_name,
                    )
                )
                return
            self._start_task = loop.create_task(danmaku.start(self._danmaku_args))
            try:
                loop.run_until_complete(self._start_task)
            except asyncio.CancelledError:
                pass
            except RuntimeError as e:
                if self._start_task is not None and self._start_task.cancelled():
                    pass
                else:
                    logger.warning(
                        i18n.tr(
                            "[弹幕采集]{cls_name} 运行异常,不影响录制: {e}",
                            cls_name=self._cls_name,
                            e=e,
                        )
                    )
            except Exception as e:
                logger.warning(
                    i18n.tr(
                        "[弹幕采集]{cls_name} 运行异常,不影响录制: {e}",
                        cls_name=self._cls_name,
                        e=e,
                    )
                )
        finally:
            logger.debug(
                i18n.tr(
                    "[弹幕采集]{cls_name} 采集线程已退出,共收到 {msg_count} 条消息",
                    cls_name=self._cls_name,
                    msg_count=self._msg_count,
                )
            )
            pending = self._start_task
            if pending is not None and not pending.done():
                pending.cancel()
                drain_deadline = time.monotonic() + _LOOP_RUNNING_WAIT_SECONDS
                try:
                    while not pending.done() and time.monotonic() < drain_deadline:
                        loop.run_until_complete(asyncio.sleep(0.01))
                except Exception:
                    pass
            try:
                loop.close()
            except Exception:
                pass
            self._loop = None
            self._start_task = None

    def _srt_writer_loop(self) -> None:
        """SRT 写盘线程主体：阻塞从 self._srt_queue 取 (user, msg, now) 并调 srt.write；收到 sentinel。"""
        srt = self._srt
        if srt is None:
            return
        _last_warn_at = 0.0
        _fail_since_warn = 0
        while True:
            try:
                item = self._srt_queue.get(timeout=_SRT_QUEUE_POLL_SECONDS)
            except queue.Empty:
                if self._srt_stop.is_set():
                    return
                continue
            if item is None:
                return

            user_name, message, now = item
            try:
                srt.write(user_name, message, now=now)
            except Exception as e:
                _fail_since_warn += 1
                _now = time.monotonic()
                if _now - _last_warn_at >= _SRT_WARN_INTERVAL:
                    logger.warning(
                        i18n.tr(
                            "[弹幕采集]SRT 写入失败(继续采集): {type_name}: {e}（本窗口累计失败 {count} 条）",
                            type_name=type(e).__name__,
                            e=e,
                            count=_fail_since_warn,
                        )
                    )
                    _last_warn_at = _now
                    _fail_since_warn = 0

    def _on_message(self, msg: DanmakuMessage) -> None:
        """收到弹幕回调：全部类型转发监控枢纽；SRT 仅记录 CHAT 且用户名或内容非空的消息。"""
        user_name = msg.user_name[:_DANMAKU_TEXT_LIMIT]
        message = msg.message[:_DANMAKU_TEXT_LIMIT]
        hub = self._monitor_hub()
        if hub is not None:
            hub.room_message(self._room_name, msg.type.value, user_name, message, data=msg.data)
        if msg.type != DanmakuMessageType.CHAT:
            return  # SRT 当前只录普通弹幕
        if not user_name and not message:
            return
        self._msg_count += 1
        now = time.monotonic()
        if self._srt is not None:
            try:
                self._srt_queue.put_nowait((user_name, message, now))
            except queue.Full:
                self._srt_dropped += 1
                if self._srt_dropped == 1 or self._srt_dropped % 1000 == 0:
                    logger.warning(
                        i18n.tr(
                            "[弹幕采集]SRT 写入队列已满，正在丢弃弹幕（累计 {count} 条）——请检查磁盘空间与写入性能",
                            count=self._srt_dropped,
                        )
                    )

    def _on_ready(self) -> None:
        """连接就绪/关闭回调：上报监控枢纽并记一条 debug 日志。"""
        hub = self._monitor_hub()
        if hub is not None:
            hub.room_connected(self._room_name)
        logger.debug(
            i18n.tr(
                "[弹幕采集]{cls_name} 连接就绪,开始接收弹幕",
                cls_name=self._cls_name,
            )
        )

    def _on_close(self, reason: str) -> None:
        """连接关闭回调：上报监控枢纽，并把关闭原因记入 debug 日志。"""
        reason = utils.mask_credentials(reason)
        hub = self._monitor_hub()
        if hub is not None:
            hub.room_closed(self._room_name, reason)
        logger.debug(
            i18n.tr(
                "[弹幕采集]{cls_name} 连接关闭: {reason}",
                cls_name=self._cls_name,
                reason=reason,
            )
        )

    @property
    def message_count(self) -> int:
        """只读属性：本次采集已写入的弹幕条数。"""
        return self._msg_count
