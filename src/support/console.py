"""终端编码、颜色与输出。"""

import ctypes
import sys


def configure_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    if sys.platform == "win32":
        try:
            _k32 = ctypes.windll.kernel32
            _k32.SetConsoleOutputCP(65001)
            _k32.SetConsoleCP(65001)
        except Exception:
            pass


class Color:
    RED: str = "\033[31m"
    GREEN: str = "\033[32m"
    YELLOW: str = "\033[33m"
    BLUE: str = "\033[34m"
    MAGENTA: str = "\033[35m"
    CYAN: str = "\033[36m"
    WHITE: str = "\033[37m"
    RESET: str = "\033[0m"

    @staticmethod
    def print_colored(text: str, color: str) -> None:
        print(f"{color}{text}{Color.RESET}")
