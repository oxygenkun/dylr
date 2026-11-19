"""异常定位辅助。"""


def get_error_line(exc: BaseException) -> str:
    """从异常 traceback 最内层取真正出错的行号；无 traceback 时返回 "unknown"。"""
    tb = exc.__traceback__
    if not tb:
        return "unknown"
    while tb.tb_next is not None:
        tb = tb.tb_next
    return str(tb.tb_lineno)
