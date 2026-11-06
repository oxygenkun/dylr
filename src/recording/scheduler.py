import time
from collections import deque
from threading import Condition, Lock, Thread, current_thread

from src.support import i18n
from src.support.logger import logger

_PROBE_LEASE_SECONDS = 60.0


class ResizableSemaphore:
    def __init__(self, value: int) -> None:
        """可运行时调容的信号量，实现上下文管理器协议。"""
        self._cond = Condition()
        self._capacity = max(0, int(value))
        self._used = 0

    def __enter__(self) -> ResizableSemaphore:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.release()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        with self._cond:
            if not blocking:
                if self._used + 1 > self._capacity:
                    return False
                self._used += 1
                return True
            endtime: float | None = None
            if timeout >= 0:
                endtime = time.monotonic() + timeout
            while self._used + 1 > self._capacity:
                if endtime is not None:
                    remaining = endtime - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._cond.wait(remaining)
                else:
                    self._cond.wait()
            self._used += 1
            return True

    def release(self) -> None:
        with self._cond:
            if self._used > 0:
                self._used -= 1
            self._cond.notify()

    def set_value(self, new_value: int) -> None:
        new_value = max(0, int(new_value))
        with self._cond:
            self._capacity = new_value
            free = new_value - self._used
            if free > 0:
                self._cond.notify(free)

    @property
    def value(self) -> int:
        with self._cond:
            return max(0, self._capacity - self._used)

    @property
    def capacity(self) -> int:
        with self._cond:
            return self._capacity


class PlatformBreaker:
    def __init__(
        self,
        name: str,
        *,
        window: int = 40,
        fail_rate: float = 0.5,
        cooldown: float = 45.0,
        min_samples: int = 8,
    ) -> None:
        """按 key 的熔断器：closed（放行）→ open（熔断，跳过探测并退避）→ half-open（放一个探针）。"""
        self.name = name
        self._window_size = max(1, int(window))
        self._fail_rate = min(1.0, max(0.0, float(fail_rate)))
        self._cooldown = max(0.0, float(cooldown))
        self._min_samples = max(1, int(min_samples))
        self._lock = Lock()
        self._samples: deque[int] = deque(maxlen=self._window_size)
        self._fail_count = 0
        self._state = "closed"  # "closed" | "open" | "half-open"
        self._open_until = 0.0
        self._probing = False
        self._probe_granted_at = 0.0
        self._probe_seq = 0
        self._probe_owner: Thread | None = None

    def _push_sample(self, failed: int) -> None:
        if len(self._samples) == self._window_size:
            self._fail_count -= self._samples[0]
        self._samples.append(failed)
        self._fail_count += failed

    def _grant_probe(self, now: float) -> None:
        self._probing = True
        self._probe_seq += 1
        self._probe_owner = current_thread()
        self._probe_granted_at = now

    def _end_probe(self) -> None:
        self._probing = False
        self._probe_owner = None

    def record(self, success: bool) -> None:
        with self._lock:
            self._push_sample(0 if success else 1)
            if self._state == "closed":
                if len(self._samples) >= self._min_samples and (
                    self._fail_count / len(self._samples) >= self._fail_rate
                ):
                    self._state = "open"
                    self._open_until = time.monotonic() + self._cooldown
                    self._end_probe()
            elif self._state == "half-open":
                if not self._probing or self._probe_owner is not current_thread():
                    return
                self._end_probe()
                if success:
                    self._state = "closed"
                    self._samples.clear()
                    self._fail_count = 0
                else:
                    self._state = "open"
                    self._open_until = time.monotonic() + self._cooldown

    def allow(self) -> bool:
        """open 状态：等待 cooldown 结束，由 allow() 转入 half-open。"""
        with self._lock:
            if self._state == "closed":
                return True
            now = time.monotonic()
            if self._state == "open":
                if now >= self._open_until:
                    if not self._probing:
                        self._state = "half-open"
                        self._grant_probe(now)
                        return True
                    return False
                return False
            if not self._probing:
                self._grant_probe(now)
                return True
            if now - self._probe_granted_at >= _PROBE_LEASE_SECONDS:
                self._grant_probe(now)
                return True
            return False

    def backoff_seconds(self) -> float:
        with self._lock:
            if self._state == "open":
                return max(0.0, self._open_until - time.monotonic()) + 5.0
            return 5.0

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def error_rate(self) -> float:
        with self._lock:
            if not self._samples:
                return 0.0
            return self._fail_count / len(self._samples)


class ConcurrencyScheduler:
    def __init__(
        self,
        *,
        configured_limit: int = 3,
        min_capacity: int = 1,
        max_capacity: int = 128,
        scale_divisor: int = 4,
        error_rate_floor: float = 0.5,
        error_window: int = 60,
    ) -> None:
        """录制并发与资源调度中枢。"""
        self._configured_limit = max(1, int(configured_limit))
        self._min_capacity = max(1, int(min_capacity))
        self._max_capacity = max(self._min_capacity, int(max_capacity))
        self._scale_divisor = max(1, int(scale_divisor))
        self._error_rate_floor = min(1.0, max(0.0, float(error_rate_floor)))
        self._error_window_size = max(1, int(error_window))

        self._RECORDING_UNLIMITED = 4096  # 录制并发「不限制」时的高容量上限
        self._lock = Lock()
        self._active_count = 0

        self._dynamic_mode = True
        self._mode_announced = False

        self._global_errors: deque[int] = deque(maxlen=self._error_window_size)
        self._global_error_count = 0

        self._network_semaphore = ResizableSemaphore(self._compute_capacity())
        self._recording_semaphore = ResizableSemaphore(self._RECORDING_UNLIMITED)
        self._recording_limit = 0  # 0 = 不限制

        self._breakers: dict[str, PlatformBreaker] = {}
        self._breakers_lock = Lock()

    def _compute_capacity(self) -> int:
        """—— 容量计算 ——。"""
        with self._lock:
            dynamic = self._dynamic_mode
            configured = self._configured_limit
            active = self._active_count
            if self._global_errors:
                rate = self._global_error_count / len(self._global_errors)
            else:
                rate = 0.0
        if not dynamic:
            return configured
        target = max(
            configured,
            min(self._max_capacity, (active + self._scale_divisor - 1) // self._scale_divisor),
        )
        if rate >= self._error_rate_floor:
            target = max(self._min_capacity, int(target * 0.6))
        return max(self._min_capacity, target)

    def recompute(self) -> None:
        new_cap = self._compute_capacity()
        if new_cap != self._network_semaphore.capacity:
            self._network_semaphore.set_value(new_cap)
            with self._lock:
                dynamic = self._dynamic_mode
                active = self._active_count
            if dynamic:
                logger.debug(
                    i18n.tr(
                        "并发模式: 动态调速，网络容量调整为 {new_cap}（活跃任务 {active}）",
                        new_cap=new_cap,
                        active=active,
                    )
                )
            else:
                logger.debug(
                    i18n.tr(
                        "并发模式: 固定，网络容量调整为 {new_cap}（来源: 同一时间访问网络的线程数）", new_cap=new_cap
                    )
                )

    def set_active_count(self, n: int) -> None:
        """—— 配置入口 ——。"""
        with self._lock:
            self._active_count = max(0, int(n))
        self.recompute()

    def set_configured_limit(self, n: int) -> None:
        with self._lock:
            self._configured_limit = max(1, int(n))
        self.recompute()

    def set_recording_limit(self, n: int) -> None:
        self._recording_limit = max(0, int(n))
        if self._recording_limit > 0:
            self._recording_semaphore.set_value(self._recording_limit)
        else:
            self._recording_semaphore.set_value(self._RECORDING_UNLIMITED)

    def set_dynamic_mode(self, enabled: bool) -> None:
        enabled = bool(enabled)
        with self._lock:
            if enabled == self._dynamic_mode and self._mode_announced:
                return
            self._dynamic_mode = enabled
            self._mode_announced = True
        self.recompute()
        if enabled:
            logger.debug(
                i18n.tr(
                    "并发模式: 动态调速（网络容量随活跃任务数自适应，当前 {value}，下限 {min_capacity}，上限 {max_capacity}）",
                    value=self._network_semaphore.capacity,
                    min_capacity=self._min_capacity,
                    max_capacity=self._max_capacity,
                )
            )
        else:
            logger.debug(
                i18n.tr(
                    "并发模式: 固定（忽略动态调速器，网络容量固定为 {value}，来源: 配置「同一时间访问网络的线程数」）",
                    value=self._network_semaphore.capacity,
                )
            )

    def _breaker(self, key: str) -> PlatformBreaker:
        """—— 熔断器 ——。"""
        with self._breakers_lock:
            b = self._breakers.get(key)
            if b is None:
                b = PlatformBreaker(key)
                self._breakers[key] = b
            return b

    def allow(self, key: str) -> bool:
        return self._breaker(key).allow()

    def backoff_seconds(self, key: str) -> float:
        return self._breaker(key).backoff_seconds()

    def record_success(self, key: str | None = None) -> None:
        """—— 错误/成功计数（驱动熔断与全局背压）——。"""
        if key:
            self._breaker(key).record(True)
        with self._lock:
            self._push_global_error(0)

    def record_failure(self, key: str | None = None) -> None:
        if key:
            self._breaker(key).record(False)
        with self._lock:
            self._push_global_error(1)

    def _push_global_error(self, failed: int) -> None:
        if len(self._global_errors) == self._error_window_size:
            self._global_error_count -= self._global_errors[0]
        self._global_errors.append(failed)
        self._global_error_count += failed

    @property
    def network_semaphore(self) -> ResizableSemaphore:
        """—— 暴露信号量（供 `with scheduler.network_semaphore:` 直接使用）——。"""
        return self._network_semaphore

    @property
    def recording_semaphore(self) -> ResizableSemaphore:
        return self._recording_semaphore

    @property
    def recording_limit(self) -> int:
        return self._recording_limit

    @property
    def dynamic_mode(self) -> bool:
        return self._dynamic_mode

    def adjust_loop(self) -> None:
        """—— 守护循环（取代旧 adjust_max_request）——。"""
        while True:
            time.sleep(5)
            try:
                self.recompute()
            except Exception as e:  # 守护循环自身不应因异常退出
                logger.debug(i18n.tr("并发调度重算异常（已忽略）: {type_name}: {e}", type_name=type(e).__name__, e=e))


def host_of(url: str) -> str:
    try:
        tail = url.split("://", 1)[-1]
        host = tail.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        host = host.rsplit("@", 1)[-1]
        host = host.lower()
        if not host:
            return "unknown"
        return host
    except Exception:
        return "unknown"


__all__ = [
    "ResizableSemaphore",
    "PlatformBreaker",
    "ConcurrencyScheduler",
    "host_of",
]
