"""签名脚本校验、JS 执行与子进程回收。"""

import asyncio
import hashlib
import inspect
import json
import os
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, cast

from src.runtime.resources import RESOURCES
from src.support import i18n
from src.support.logger import logger

try:
    import exejs as execjs

    ProgramError = execjs.ExejsProgramError
except ImportError:
    import execjs  # type: ignore[no-redef]
    from execjs import ProgramError  # type: ignore[no-redef]


_js_compile_cache: dict[str, tuple[float, object]] = {}

_js_compile_lock = threading.Lock()


_JS_SHA256_EXPECTED: dict[str, str] = RESOURCES["javascript_sha256"]


_js_unpinned_warned: set[str] = set()


_js_unpinned_warned_lock = threading.Lock()


def _js_unpinned_first_seen(name: str) -> bool:
    with _js_unpinned_warned_lock:
        if name in _js_unpinned_warned:
            return False
        _js_unpinned_warned.add(name)
        return True


def _check_js_hash(js_path: str, raw: bytes) -> None:
    name = os.path.basename(js_path)
    expected = _JS_SHA256_EXPECTED.get(name)
    unpinned = expected is None
    if unpinned and not _js_unpinned_first_seen(name):
        return
    actual = hashlib.sha256(raw).hexdigest()
    if not unpinned and actual == expected:
        return
    shown_expected = "未登记（_JS_SHA256_EXPECTED 无该键，本次已跳过校验）" if unpinned else str(expected)
    strict = os.environ.get("DLR_JS_STRICT_HASH", "").strip().lower() in ("1", "true", "yes")
    if strict and not unpinned:
        raise RuntimeError(
            f"签名脚本 {name} 的 SHA256 与钉定值不符，已按 DLR_JS_STRICT_HASH=1 拒绝执行"
            f"（期望 {shown_expected}，实际 {actual}）"
        )
    logger.warning(
        i18n.tr(
            "签名脚本 {name} 的 SHA256 与钉定值不符（期望 {expected}，实际 {actual}）；"
            "若你刚更新过该脚本请同步更新 src/external_resources.json，否则请核查文件是否被篡改",
            name=name,
            expected=shown_expected,
            actual=actual,
        )
    )


def get_compiled_js(js_path: str) -> object:
    try:
        mtime = os.path.getmtime(js_path)
    except OSError:
        mtime = 0.0
    with _js_compile_lock:
        cached = _js_compile_cache.get(js_path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        with open(js_path, "rb") as f:
            raw = f.read()
        _check_js_hash(js_path, raw)
        source = raw.decode("utf-8")
        compiled = execjs.compile(source)
        _js_compile_cache[js_path] = (mtime, compiled)
        return compiled


def _js_runtime_accepts_timeout(call_fn: Callable[..., object]) -> bool:
    try:
        parameters = inspect.signature(call_fn).parameters
    except TypeError, ValueError:  # pragma: no cover - C 实现等取不到签名的情形
        return False
    if "timeout" in parameters:
        return True
    return any(param.kind is inspect.Parameter.VAR_KEYWORD for param in parameters.values())


async def run_js_async(js_path: str, func_name: str, *args: object, timeout: float = 30.0) -> object:
    """在线程池中执行签名函数，并限制等待时间。"""

    def _sync_call() -> object:
        call_fn = cast("Callable[..., object]", getattr(get_compiled_js(js_path), "call"))
        kwargs: dict[str, float] = {"timeout": timeout} if _js_runtime_accepts_timeout(call_fn) else {}
        return cast(object, call_fn(func_name, *args, **kwargs))

    return await asyncio.wait_for(
        asyncio.get_running_loop().run_in_executor(_js_executor(), _sync_call), timeout=timeout
    )


_JS_EXECUTOR: object | None = None


_JS_EXECUTOR_LOCK = threading.Lock()


def _js_executor() -> "ThreadPoolExecutor":
    global _JS_EXECUTOR
    with _JS_EXECUTOR_LOCK:
        if _JS_EXECUTOR is None:
            _JS_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="js-call")
        return cast("ThreadPoolExecutor", _JS_EXECUTOR)


_NODE_TREE_KILL_VIA_TASKKILL = os.name == "nt"


_NODE_TREE_KILL_TIMEOUT_SECONDS = 5.0


_NODE_REAP_TIMEOUT_SECONDS = 5.0


def _kill_posix_process_group(pid: int) -> None:
    """终止 Node 所属的 POSIX 进程组。"""
    if sys.platform == "win32":
        return
    os.killpg(os.getpgid(pid), signal.SIGKILL)


def _kill_node_process_tree(process: subprocess.Popen[bytes], script_name: str) -> None:
    """回收 Node 进程树并限时等待退出。"""
    try:
        if _NODE_TREE_KILL_VIA_TASKKILL:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                capture_output=True,
                timeout=_NODE_TREE_KILL_TIMEOUT_SECONDS,
                check=False,
            )
        else:
            _kill_posix_process_group(process.pid)
    except Exception as e:
        logger.warning(
            i18n.tr(
                "回收 node 签名子进程树失败: {script} - {type_name}: {e}",
                script=script_name,
                type_name=type(e).__name__,
                e=e,
            )
        )
    finally:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=_NODE_REAP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            pass


async def run_node_script_async(script_path: str, *args: str, timeout: float = 30.0) -> str:
    """校验脚本后启动 Node 子进程，并在超时时回收进程树。"""

    def _sync_run() -> str:
        with open(script_path, "rb") as f:
            raw = f.read()
        _check_js_hash(script_path, raw)
        if Path(script_path).name == "migu.js":
            raw = f"const DLR_MIGU_RESOURCES = {json.dumps(RESOURCES['migu'])};\n".encode() + raw
        argv = ["node", "-", *args]
        if _NODE_TREE_KILL_VIA_TASKKILL:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        else:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        try:
            stdout, _stderr = proc.communicate(input=raw, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_node_process_tree(proc, os.path.basename(script_path))
            raise
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, argv)
        out: bytes = stdout or b""
        return out.decode("utf-8").strip()

    return await asyncio.wait_for(asyncio.to_thread(_sync_run), timeout=timeout)
