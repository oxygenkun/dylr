"""平台解析异常记录与返回值兜底。"""

import functools
import inspect
import traceback
from collections.abc import Callable
from typing import cast

from src.runtime.javascript import ProgramError
from src.support import i18n
from src.support.logger import logger
from src.support.utils import mask_credentials


def _make_trace_error_guard[**P, R](func: Callable[P, R], fallback_factory: Callable[[], R]) -> Callable[P, R]:
    fallback_type_name = type(fallback_factory()).__name__

    def handle_error(error: Exception) -> R:
        if isinstance(error, ProgramError):
            logger.warning(
                i18n.tr(
                    "JS 执行环境异常（Node.js 缺失或签名脚本执行失败）: 类型: {type_name}, {masked} in function {func_name}",
                    type_name=type(error).__name__,
                    masked=mask_credentials(str(error)),
                    func_name=func.__name__,
                )
            )
        else:
            tb = traceback.extract_tb(error.__traceback__) if error.__traceback__ else None
            error_line = tb[-1].lineno if tb else "unknown"
            error_info = (
                f"message: type: {type(error).__name__}, {mask_credentials(str(error))} in function {func.__name__}"
                f" at line: {error_line}, fallback type: {fallback_type_name}"
            )
            logger.error(error_info)
        return fallback_factory()

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return cast(R, await func(*args, **kwargs))
            except Exception as e:
                return handle_error(e)

        return cast(Callable[P, R], async_wrapper)

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            return handle_error(e)

    return wrapper


def trace_error_decorator[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    return _make_trace_error_guard(func, lambda: cast(R, {"is_live": False}))


def trace_error_decorator_or_none[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    return _make_trace_error_guard(func, lambda: cast(R, None))
