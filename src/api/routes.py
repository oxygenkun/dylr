from __future__ import annotations

import asyncio
import configparser
import inspect
import json
import os
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path
from typing import cast

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.staticfiles import StaticFiles

from src.api.config import (
    BUILTIN_QUALITIES,
    QUALITY_OPTIONS_KEY,
    QUALITY_OPTIONS_SECTION,
    SENSITIVE_MASK,
    TEXT_ENCODING,
    _looks_like_secret_value,
    format_url_line,
    hash_web_password,
    is_hashed_web_password,
    is_host_allowed,
    is_loopback_bind_host,
    is_origin_allowed,
    is_sensitive_item,
    normalize_url,
    parse_config_bool,
    parse_url_config,
    read_config_safe,
    read_quality_options,
    read_web_config,
    update_config_line,
    update_or_append_config_line,
    update_room_quality,
    validate_config_target,
    validate_room_target,
    verify_web_password,
    write_quality_options,
)
from src.api.models import (
    ConfigUpdate,
    LanguageUpdate,
    LoginRequest,
    QualityOptionsUpdate,
    RecordingToggle,
    RoomCreate,
    RoomQualityUpdate,
    RoomToggle,
    RoomUpdate,
)
from src.config import io as config_io
from src.recording import state
from src.recording import status as recorder_status
from src.support import i18n as i18n_module
from src.support.logger import logger

_BODY_MODELS = {
    LoginRequest,
    RoomCreate,
    RoomUpdate,
    RoomToggle,
    RoomQualityUpdate,
    QualityOptionsUpdate,
    RecordingToggle,
    ConfigUpdate,
    LanguageUpdate,
}

_QUERY_DEFAULTS: dict[str, dict[str, tuple[object, int | None, int | None]]] = {
    "delete_room": {"url": ("", None, None)},
    "list_files": {"path": ("", None, None)},
    "download_file": {"path": ("", None, None)},
    "get_logs": {"lines": (200, 1, 5000)},
    "get_danmaku": {"since": (0, 0, None)},
}


_JSON_BODY_MAX_BYTES = 1 * 1024 * 1024


async def _read_json_body(request: Request) -> object:
    content_length = request.headers.get("content-length", "")
    if content_length.isdigit() and int(content_length) > _JSON_BODY_MAX_BYTES:
        raise HTTPException(status_code=413, detail=i18n_module.tr("请求体过大"))
    try:
        return await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail=i18n_module.tr("请求体不是合法 JSON"))


def _route(app: Starlette, methods: list[str], path: str) -> Callable[[Callable[..., object]], Callable[..., object]]:
    def _decorator(handler: Callable[..., object]) -> Callable[..., object]:
        """装饰器工厂：把 FastAPI 风格的端点（模型参数 / Query 参数 / dict→JSON 返回）适配到 Starlette。"""
        sig = inspect.signature(handler, eval_str=True)
        params = sig.parameters
        is_async = inspect.iscoroutinefunction(handler)
        model_param: str | None = None
        query_names: list[str] = []
        takes_request = False
        for name, p in params.items():
            if name == "request":
                takes_request = True
            elif p.annotation in _BODY_MODELS:
                model_param = name
            else:
                query_names.append(name)

        async def _adapter(request: Request) -> Response:
            kwargs: dict[str, object] = {}
            if model_param is not None:
                payload = await _read_json_body(request)
                model_cls = params[model_param].annotation
                try:
                    kwargs[model_param] = model_cls.parse(payload)
                except ValueError as exc:
                    raise HTTPException(status_code=422, detail=str(exc))
            for qname in query_names:
                default, lo, hi = _QUERY_DEFAULTS.get(handler.__name__, {}).get(qname, ("", None, None))
                raw = request.query_params.get(qname)
                if raw is None or raw == "":
                    value: object = default
                elif isinstance(default, int):
                    try:
                        value = int(raw)
                    except ValueError:
                        value = default
                else:
                    value = raw
                if isinstance(value, int):
                    if lo is not None:
                        value = max(lo, value)
                    if hi is not None:
                        value = min(hi, value)
                kwargs[qname] = value
            if takes_request:
                kwargs["request"] = request
            if is_async:
                result = await cast(Callable[..., Awaitable[object]], handler)(**kwargs)
            else:
                result = await run_in_threadpool(handler, **kwargs)
            if isinstance(result, Response):
                return result
            return JSONResponse(result)

        app.add_route(path, _adapter, methods=methods)
        return handler

    return _decorator


_WEB_DIR = Path(__file__).resolve().parents[2] / "web"

_tokens: dict[str, float] = {}
_tokens_lock = threading.Lock()
_web_cfg_cache_lock = threading.Lock()

_FAILED_LOGINS: dict[str, list[float]] = {}
_FAILED_LOGINS_LOCK = threading.Lock()
_LOGIN_MAX_FAILURES = 5
_LOGIN_FAILURE_WINDOW = 300.0
_GLOBAL_LOGIN_FAILURES: deque[float] = deque(maxlen=512)
_LOGIN_GLOBAL_MAX_FAILURES = 40

_PUBLIC_BUDGET_WINDOW = 60.0
_PUBLIC_BUDGET_MAX = 240
_PUBLIC_BUDGET_PATHS = frozenset(("/api/auth/status", "/health"))
_PUBLIC_BUDGET: dict[str, deque[float]] = {}
_PUBLIC_BUDGET_LOCK = threading.Lock()


def _public_endpoint_over_budget(client_ip: str) -> bool:
    now = time.monotonic()
    with _PUBLIC_BUDGET_LOCK:
        for stale_ip in [k for k, q in _PUBLIC_BUDGET.items() if not q or now - q[-1] > _PUBLIC_BUDGET_WINDOW]:
            _PUBLIC_BUDGET.pop(stale_ip, None)
        hits = _PUBLIC_BUDGET.get(client_ip)
        if hits is None:
            hits = deque(maxlen=_PUBLIC_BUDGET_MAX + 8)
            _PUBLIC_BUDGET[client_ip] = hits
        while hits and now - hits[0] > _PUBLIC_BUDGET_WINDOW:
            hits.popleft()
        if len(hits) >= _PUBLIC_BUDGET_MAX:
            return True
        hits.append(now)
        return False


_DANGEROUS_CONFIG_KEYS = {"自定义脚本执行命令"}
_DANGEROUS_CONFIG_KEYS_FOLDED = {k.casefold() for k in _DANGEROUS_CONFIG_KEYS}

_rooms_config_lock = threading.Lock()

_STATUS_ERROR_CODE = "status_unavailable"
_LOGS_ERROR_CODE = "logs_unavailable"
_DANMAKU_ERROR_CODE = "danmaku_unavailable"
_ROOM_DELETE_ERROR_CODE = "room_delete_failed"
_ROOM_UPDATE_ERROR_CODE = "room_update_failed"
_HOST_DENIED_DETAIL = "host_not_allowed"

_TOKEN_EXPIRY_MIN_SECONDS = 60.0
_TOKEN_EXPIRY_MAX_SECONDS = 7 * 86400.0


def _clamp_token_expiry(raw: object) -> float:
    try:
        value = float(cast(float, raw))
    except TypeError, ValueError:
        return 86400.0
    if value != value:
        return 86400.0
    return max(_TOKEN_EXPIRY_MIN_SECONDS, min(_TOKEN_EXPIRY_MAX_SECONDS, value))


_STATUS_TTL_SECONDS = 2.0
_STATUS_WAIT_SECONDS = 3.0
_STATUS_INFLIGHT_LEASE = 60.0
_status_lock = threading.Lock()
_status_cache: dict[str, object] | None = None
_status_cache_at = 0.0
_status_inflight = False
_status_inflight_since = 0.0


def _log_internal_error(code: str, exc: BaseException | None = None, level: str = "error") -> None:
    try:
        from loguru import logger

        if level == "warning":
            logger.opt(exception=exc).warning("{}", code)
        else:
            logger.opt(exception=exc).error("{}", code)
    except Exception:
        pass


_DENIED_HOSTS_SEEN: set[str] = set()
_DENIED_HOSTS_SEEN_MAX = 64


def _should_warn_denied_host(host: str) -> bool:
    key = host.strip().lower()[:120]
    if key in _DENIED_HOSTS_SEEN:
        return False
    if len(_DENIED_HOSTS_SEEN) >= _DENIED_HOSTS_SEEN_MAX:
        _DENIED_HOSTS_SEEN.clear()
    _DENIED_HOSTS_SEEN.add(key)
    return True


def _insecure_bind_allowed() -> bool:
    return os.environ.get("DOUYIN_WEB_ALLOW_INSECURE", "").strip().lower() in ("1", "true", "yes")


_INSECURE_BIND_ENV_TAIL = "；确需对局域网无认证开放时，请设置环境变量 DOUYIN_WEB_ALLOW_INSECURE=1 后重启本进程"


def _insecure_bind_detail(context: str, action: str, bind_host: str) -> str:
    return i18n_module.tr(
        "非回环监听地址（当前实际监听 {bind}）下{context}：{action}{tail}",
        bind=bind_host.strip() or i18n_module.tr("(未知)"),
        context=context,
        action=action,
        tail=i18n_module.tr(_INSECURE_BIND_ENV_TAIL),
    )


def _guard_bind_host(app: Starlette, cfg: dict[str, str | int | bool]) -> str:
    bound = cast("str | None", app.state.bind_host)
    if isinstance(bound, str) and bound.strip():
        return bound.strip()
    return str(cast(object, cfg.get("web_host", "")) or "")


def _guard_bind_port(app: Starlette) -> int:
    return cast("int | None", app.state.bind_port) or 0


def _trusted_proxy_set(cfg: dict[str, str | int | bool]) -> set[str]:
    """app.state 上的自定义属性由 Starlette 动态承载（类型化为 Any），读取处统一用 cast 收敛类型。"""
    return {h.strip() for h in str(cast(str, cfg.get("web_trusted_proxy", ""))).split(",") if h.strip()}


def _get_client_ip(request: Request, cfg: dict[str, str | int | bool]) -> str:
    peer = request.client.host if request.client else "unknown"
    trusted = _trusted_proxy_set(cfg)
    if peer not in trusted:
        return peer
    xff = request.headers.get("x-forwarded-for", "")
    for hop in reversed([h.strip() for h in xff.split(",") if h.strip()]):
        if hop not in trusted:
            return hop
    return peer


def _read_app_version() -> str:
    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if pyproject_path.exists():
        text = pyproject_path.read_text(encoding="utf-8")
        m = re.search(r'^version\s*=\s*["\'](.+?)["\']', text, re.MULTILINE)
        if m:
            return m.group(1)
    return "0.0.0"


_APP_VERSION = _read_app_version()


def create_app(
    config_file: str,
    url_config_file: str,
    downloads_root: str,
    logs_dir: str,
    bind_host: str | None = None,
    bind_port: int | None = None,
) -> Starlette:
    app = Starlette()

    async def _http_exception_to_json(request: Request, exc: Exception) -> JSONResponse:
        """FastAPI 对端点内 raise HTTPException 的默认响应是 JSONResponse({"detail": ...})，而 Starlette。"""
        http_exc = cast(HTTPException, exc)
        return JSONResponse({"detail": http_exc.detail}, status_code=http_exc.status_code, headers=http_exc.headers)

    app.add_exception_handler(HTTPException, _http_exception_to_json)

    setattr(app.state, "config_file", config_file)
    setattr(app.state, "url_config_file", url_config_file)
    setattr(app.state, "downloads_root", os.path.realpath(downloads_root))
    setattr(app.state, "logs_dir", logs_dir)
    setattr(app.state, "bind_host", bind_host)
    setattr(app.state, "bind_port", bind_port)

    async def auth_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """认证中间件：每次请求重新读取配置，保证面板内修改配置即时生效。"""
        cfg = _read_web_config_cached(cast(str, cast(Starlette, request.app).state.config_file))
        _purge_expired_tokens()
        path = request.url.path
        response: Response
        if not is_host_allowed(request.headers.get("host", ""), cfg):
            if _should_warn_denied_host(request.headers.get("host", "")):
                _log_internal_error(_HOST_DENIED_DETAIL, level="warning")
            response = JSONResponse(status_code=400, content={"detail": _HOST_DENIED_DETAIL})
        elif _api_locked_out_by_insecure_bind(cfg, path, _guard_bind_host(app, cfg)):
            response = JSONResponse(
                status_code=403,
                content={
                    "detail": "insecure_bind_disabled",
                    "message": i18n_module.tr("非回环监听地址下不允许关闭 Web 认证"),
                },
            )
        elif request.method not in ("GET", "HEAD", "OPTIONS") and not _is_same_origin(
            request, cfg, _guard_bind_host(app, cfg), _guard_bind_port(app)
        ):
            response = JSONResponse(status_code=403, content={"detail": "cross-origin request denied"})
        elif (
            not cast(bool, cfg["web_auth_enable"])
            or path in ("/api/login", "/api/logout", "/api/auth/status", "/health", "/", "/web", "/favicon.ico")
            or path.startswith("/web/")
        ):
            if path in _PUBLIC_BUDGET_PATHS and _public_endpoint_over_budget(_get_client_ip(request, cfg)):
                response = JSONResponse(status_code=429, content={"detail": "rate_limited"})
            else:
                response = await call_next(request)
        else:
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                token = auth[7:]
                with _tokens_lock:
                    exp = _tokens.get(token)
                    valid = exp is not None and exp > time.time()
                if valid:
                    response = await call_next(request)
                else:
                    response = JSONResponse(status_code=401, content={"detail": "unauthorized"})
            else:
                response = JSONResponse(status_code=401, content={"detail": "unauthorized"})
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self' data:; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'",
        )
        return response

    app.add_middleware(BaseHTTPMiddleware, dispatch=auth_middleware)

    @_route(app, ["POST"], "/api/login")
    def login(req: LoginRequest, request: Request) -> dict[str, object]:
        cfg = dict(_read_web_config_cached(cast(str, app.state.config_file)))
        if not cast(bool, cfg["web_auth_enable"]):
            return {"token": "", "expires_in": 0, "auth_required": False}
        client_ip = _get_client_ip(request, cfg)
        now = time.time()
        with _FAILED_LOGINS_LOCK:
            while _GLOBAL_LOGIN_FAILURES and now - _GLOBAL_LOGIN_FAILURES[0] > _LOGIN_FAILURE_WINDOW:
                _ = _GLOBAL_LOGIN_FAILURES.popleft()
            if len(_GLOBAL_LOGIN_FAILURES) >= _LOGIN_GLOBAL_MAX_FAILURES:
                raise HTTPException(429, i18n_module.tr("登录失败次数过多，请稍后再试"))
            for ip in [ip for ip, ts in _FAILED_LOGINS.items() if not any(now - t < _LOGIN_FAILURE_WINDOW for t in ts)]:
                _FAILED_LOGINS.pop(ip, None)
            failures = [t for t in _FAILED_LOGINS.get(client_ip, []) if now - t < _LOGIN_FAILURE_WINDOW]
            _FAILED_LOGINS[client_ip] = failures
            if len(failures) >= _LOGIN_MAX_FAILURES:
                raise HTTPException(429, i18n_module.tr("登录失败次数过多，请稍后再试"))
        _purge_expired_tokens()
        if not cast(str, cfg["web_password"]):
            raise HTTPException(500, i18n_module.tr("web_password 未配置但认证已开启"))
        if not is_hashed_web_password(cast(str, cfg["web_password"])):
            hashed = hash_web_password(cast(str, cfg["web_password"]).strip())

            with state.file_update_lock:
                _ = update_config_line(cast(str, app.state.config_file), "Web", "web_password", hashed)
            _invalidate_web_cfg_cache()
            cfg["web_password"] = hashed
        if not verify_web_password(req.password.strip(), cast(str, cfg["web_password"])):
            with _FAILED_LOGINS_LOCK:
                _FAILED_LOGINS.setdefault(client_ip, []).append(time.time())
                _GLOBAL_LOGIN_FAILURES.append(time.time())
            raise HTTPException(401, i18n_module.tr("密码错误"))
        with _FAILED_LOGINS_LOCK:
            _FAILED_LOGINS.pop(client_ip, None)
        token = secrets.token_urlsafe(32)
        expires_in = _clamp_token_expiry(cfg["web_token_expiry"])
        expiry = time.time() + expires_in
        with _tokens_lock:
            _tokens[token] = expiry
        return {"token": token, "expires_in": expires_in}

    @_route(app, ["POST"], "/api/logout")
    def logout(request: Request) -> dict[str, object]:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            with _tokens_lock:
                _ = _tokens.pop(auth[7:], None)
        return {"ok": True}

    @_route(app, ["GET"], "/api/status")
    async def get_status() -> dict[str, object]:
        snapshot, stale = await _get_status_snapshot()
        if stale:
            return {**snapshot, "stale": True}
        return snapshot

    @_route(app, ["GET"], "/api/auth/status")
    def auth_status() -> dict[str, object]:
        """公开端点：暴露 Web 认证状态，供前端在登录前提示「当前面板未启用认证」。"""
        cfg = _read_web_config_cached(cast(str, app.state.config_file))
        auth_enabled = cast(bool, cfg["web_auth_enable"])
        return {
            "auth_required": auth_enabled,
            "warning": (
                None if auth_enabled else i18n_module.tr("Web 认证未启用，任何能访问本面板的网络均可操控录制配置")
            ),
        }

    @_route(app, ["GET"], "/health")
    async def health() -> dict[str, str]:
        """探活端点：进程存活 + ASGI/路由已就绪 + 应用版本可读（_APP_VERSION 在模块导入期即从。"""
        return {"status": "ok", "version": _APP_VERSION}

    @_route(app, ["GET"], "/api/status/stream")
    async def status_stream(request: Request) -> StreamingResponse:
        _MAX_CONSECUTIVE_ERRORS = 5

        async def event_gen() -> AsyncGenerator[str, None]:
            consecutive_errors = 0
            while True:
                if await request.is_disconnected():
                    return
                snapshot, _stale = await _get_status_snapshot()
                if _STATUS_ERROR_CODE in snapshot:
                    consecutive_errors += 1
                    yield f"data: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
                    if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                        return
                else:
                    consecutive_errors = 0
                    yield f"data: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
                await asyncio.sleep(2)

        return StreamingResponse(event_gen(), media_type="text/event-stream")

    @_route(app, ["POST"], "/api/recording/toggle")
    def toggle_recording(req: RecordingToggle) -> dict[str, object]:

        state.recording_enabled = req.enable
        if not req.enable:
            from src.app.log_archive import archive_runtime_logs

            _ = archive_runtime_logs(reopen_streams=True)
        return {"ok": True, "recording_enabled": req.enable}

    @_route(app, ["GET"], "/api/rooms")
    def list_rooms() -> list[dict[str, object]]:
        rooms: list[dict[str, object]] = cast(
            list[dict[str, object]], parse_url_config(cast(str, app.state.url_config_file))
        )
        running: list[str] | None
        try:
            with state.record_state_lock:
                running = list(state.running_list)
        except Exception as e:
            running = None
            logger.warning(
                i18n_module.tr(
                    "读取录制状态失败，房间列表中的录制态标记为未知: {type_name}: {e}",
                    type_name=type(e).__name__,
                    e=e,
                )
            )
        for r in rooms:
            r["recording"] = None if running is None else any(r["url"] == u for u in running)
        return rooms

    @_route(app, ["POST"], "/api/rooms")
    def add_room(req: RoomCreate) -> dict[str, object]:
        url = normalize_url(req.url)
        try:
            validate_room_target(req.url, req.quality, req.name)
            line = format_url_line(url, req.quality, req.name)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

        with _rooms_config_lock, state.file_update_lock:
            existing = parse_url_config(cast(str, app.state.url_config_file))
            if any(r["url"] == url for r in existing):
                raise HTTPException(409, i18n_module.tr("直播间已存在"))
            _url_cfg_path = cast(str, app.state.url_config_file)
            _needs_newline = False
            _is_fresh_file = False
            try:
                with open(_url_cfg_path, "rb") as rf:
                    _ = rf.seek(0, 2)  # 2 = SEEK_END
                    if rf.tell() > 0:
                        _ = rf.seek(-1, 2)
                        _needs_newline = rf.read(1) not in (b"\n", b"\r")
                    else:
                        _is_fresh_file = True  # 存在但为 0 字节：与新建同处理
            except FileNotFoundError:
                _is_fresh_file = True  # 尚不存在：由本次追加建文件，须带文件头 BOM
            except OSError:
                pass
            with open(_url_cfg_path, "a", encoding="utf-8") as f:
                if _is_fresh_file:
                    _ = f.write("\ufeff")
                _ = f.write(("\n" if _needs_newline else "") + line + "\n")
        return {"ok": True}

    @_route(app, ["PUT"], "/api/rooms")
    def update_room(req: RoomUpdate) -> dict[str, object]:
        old_url = normalize_url(req.old_url)
        try:
            validate_room_target(req.url, req.quality, req.name)
            new_line = format_url_line(normalize_url(req.url), req.quality, req.name)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

        replaced = False
        with _rooms_config_lock, state.file_update_lock:
            old_rooms = parse_url_config(cast(str, app.state.url_config_file))
            new_url = normalize_url(req.url)
            if any(r["url"] == new_url and r["url"] != old_url for r in old_rooms):
                raise HTTPException(409, i18n_module.tr("直播间已存在"))
            for r in old_rooms:
                if r["url"] == old_url:
                    old_raw = cast(str, r["raw_line"]).rstrip("\n").rstrip("\r")
                    prefix = "# " if not r["enabled"] else ""
                    new_raw = (prefix + new_line) if prefix else new_line
                    _ = config_io.update_file(
                        cast(str, app.state.url_config_file),
                        old_str=old_raw,
                        new_str=new_raw,
                    )
                    after = parse_url_config(cast(str, app.state.url_config_file))
                    target = next((rr for rr in after if rr["url"] == normalize_url(req.url)), None)
                    if target is None or cast(str, target["raw_line"]).rstrip("\n").rstrip("\r") != new_raw:
                        _log_internal_error(_ROOM_UPDATE_ERROR_CODE)
                        raise HTTPException(500, _ROOM_UPDATE_ERROR_CODE)
                    replaced = True
                    break
        if not replaced:
            raise HTTPException(404, i18n_module.tr("未找到原直播间"))
        return {"ok": True}

    @_route(app, ["DELETE"], "/api/rooms")
    def delete_room(url: str = "") -> dict[str, object]:
        url = normalize_url(url)

        with _rooms_config_lock, state.file_update_lock:
            rooms = parse_url_config(cast(str, app.state.url_config_file))
            for r in rooms:
                if r["url"] == url:
                    removed = config_io.delete_line(cast(str, app.state.url_config_file), cast(str, r["raw_line"]))
                    remaining = parse_url_config(cast(str, app.state.url_config_file))
                    still_there = any(rr["url"] == url for rr in remaining)
                    if still_there or removed is False:
                        _log_internal_error(_ROOM_DELETE_ERROR_CODE)
                        raise HTTPException(500, _ROOM_DELETE_ERROR_CODE)
                    return {"ok": True}
        raise HTTPException(404, i18n_module.tr("未找到直播间"))

    @_route(app, ["POST"], "/api/rooms/toggle")
    def toggle_room(req: RoomToggle) -> dict[str, object]:
        url = normalize_url(req.url)

        with _rooms_config_lock, state.file_update_lock:
            rooms = parse_url_config(cast(str, app.state.url_config_file))
            for r in rooms:
                if r["url"] == url:
                    old_raw = cast(str, r["raw_line"]).rstrip("\n").rstrip("\r")
                    content = old_raw.lstrip("#").strip()
                    new_raw = content if req.enable else "# " + content
                    _ = config_io.update_file(
                        cast(str, app.state.url_config_file),
                        old_str=old_raw,
                        new_str=new_raw,
                    )
                    after = parse_url_config(cast(str, app.state.url_config_file))
                    target = next((rr for rr in after if rr["url"] == url), None)
                    if target is None or cast(str, target["raw_line"]).rstrip("\n").rstrip("\r") != new_raw:
                        _log_internal_error(_ROOM_UPDATE_ERROR_CODE)
                        raise HTTPException(500, _ROOM_UPDATE_ERROR_CODE)
                    return {"ok": True, "enabled": req.enable}
        raise HTTPException(404, i18n_module.tr("未找到直播间"))

    @_route(app, ["PUT"], "/api/rooms/quality")
    def change_room_quality(req: RoomQualityUpdate) -> dict[str, object]:
        """按房间切换画质：通过 update_room_quality 落盘。"""
        url = normalize_url(req.url)
        quality = (req.quality or "").strip()
        try:
            validate_room_target(req.url, quality)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        if quality and quality not in BUILTIN_QUALITIES:
            raise HTTPException(422, i18n_module.tr("未知画质档位: {quality}", quality=quality))

        with _rooms_config_lock, state.file_update_lock:
            rooms = parse_url_config(cast(str, app.state.url_config_file))
            if not any(r["url"] == url for r in rooms):
                raise HTTPException(404, i18n_module.tr("未找到直播间"))
            changed = update_room_quality(cast(str, app.state.url_config_file), url, quality)
        return {"ok": True, "changed": changed}

    @_route(app, ["GET"], "/api/rooms/qualities")
    def list_quality_options() -> dict[str, object]:
        """画质选项：Web 直播间设置的画质下拉读取这份列表，。"""
        return {
            "options": read_quality_options(cast(str, app.state.config_file)),
            "builtin": list(BUILTIN_QUALITIES),
        }

    @_route(app, ["PUT"], "/api/rooms/qualities")
    def update_quality_options(req: QualityOptionsUpdate) -> dict[str, object]:
        try:
            validate_config_target(QUALITY_OPTIONS_SECTION, QUALITY_OPTIONS_KEY, ",".join(req.options))
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

        with state.file_update_lock:
            options = write_quality_options(cast(str, app.state.config_file), req.options)
        if options is None:
            raise HTTPException(
                500,
                i18n_module.tr("写入配置文件失败（已保留原文件）: {err}", err=cast(str, app.state.config_file)),
            )
        return {"ok": True, "options": options}

    @_route(app, ["GET"], "/api/config")
    def get_config() -> dict[str, dict[str, str]]:
        return read_config_safe(cast(str, app.state.config_file))

    @_route(app, ["PUT"], "/api/config")
    def update_config(req: ConfigUpdate) -> dict[str, object]:
        key = req.key.strip()
        section = req.section.strip()
        key_norm = key.strip().lower()
        is_web_section = section.strip().lower() == "web"
        if key.casefold() in _DANGEROUS_CONFIG_KEYS_FOLDED:
            raise HTTPException(403, i18n_module.tr("该配置项不允许通过 Web 修改"))
        current_cfg: dict[str, str | int | bool] | None = None
        if is_web_section and key_norm in ("web_auth_enable", "web_password", "web_host"):
            current_cfg = dict(read_web_config(cast(str, app.state.config_file)))
            _currently_enabled = cast(bool, current_cfg["web_auth_enable"])
            if key_norm == "web_host":
                _bind_host = _guard_bind_host(app, current_cfg)
                if not _currently_enabled and not _insecure_bind_allowed():
                    if is_loopback_bind_host(req.value) and not is_loopback_bind_host(_bind_host):
                        raise HTTPException(
                            403,
                            _insecure_bind_detail(
                                i18n_module.tr("不允许把 web_host 改写为回环地址"),
                                i18n_module.tr(
                                    "改这个键不会让进程收回已监听的地址、只会让配置比现实更乐观（伪安全态）；"
                                    "请先开启 Web 认证，或改完后重启本进程使监听地址与配置一致"
                                ),
                                _bind_host,
                            ),
                        )
            else:
                _current_pwd = str(cast(object, current_cfg.get("web_password") or "")).strip()
                if key_norm == "web_auth_enable":
                    _target_enabled = parse_config_bool(req.value, False)
                    _target_pwd = _current_pwd
                else:
                    _target_enabled = _currently_enabled
                    _target_pwd = req.value.strip()
                if _target_enabled and not _target_pwd:
                    raise HTTPException(400, i18n_module.tr("开启 Web 认证必须同时设置 web_password"))
                if key_norm == "web_auth_enable" and not _target_enabled:
                    _bind = _guard_bind_host(app, current_cfg)
                    if not is_loopback_bind_host(_bind) and not _insecure_bind_allowed():
                        raise HTTPException(
                            403,
                            _insecure_bind_detail(
                                i18n_module.tr("不允许关闭 Web 认证"),
                                i18n_module.tr(
                                    "请先在 config.ini 把 web_host 改为 127.0.0.1 并重启本进程"
                                    "（判定以进程实际监听地址为准，只改配置不重启不生效）"
                                ),
                                _bind,
                            ),
                        )
        if is_web_section and key_norm == "web_password" and not req.value.strip():
            if current_cfg is None:
                current_cfg = read_web_config(cast(str, app.state.config_file))
            if cast(bool, current_cfg["web_auth_enable"]):
                raise HTTPException(400, i18n_module.tr("请先关闭 Web 认证再清空密码"))
        value = req.value
        old_value = ""
        try:
            _raw_parser = configparser.ConfigParser(interpolation=None)
            _ = _raw_parser.read(cast(str, app.state.config_file), encoding=TEXT_ENCODING)
            if _raw_parser.has_option(section, key):
                old_value = str(_raw_parser.get(section, key) or "")
        except Exception:
            old_value = ""
        if is_sensitive_item(section, key) or _looks_like_secret_value(value) or _looks_like_secret_value(old_value):
            if not value.strip():
                raise HTTPException(
                    400, i18n_module.tr("敏感配置项不得清空：如需移除请显式填写占位值并手工编辑 config.ini")
                )
            if value.strip() == SENSITIVE_MASK:
                raise HTTPException(400, i18n_module.tr("敏感配置项不得以掩码值提交：请填入真实值，或保持该项不修改"))
        if is_web_section and key_norm in ("web_auth_enable", "web_password"):
            if current_cfg is None:
                current_cfg = dict(_read_web_config_cached(cast(str, app.state.config_file)))
            if cast(bool, current_cfg["web_auth_enable"]):
                _reauth = (req.reauth_password or "").strip()
                _stored_pwd = str(cast(object, current_cfg["web_password"]) or "")
                if not _reauth or not verify_web_password(_reauth, _stored_pwd):
                    _log_internal_error(f"auth_reauth_denied: key={key_norm}", level="warning")
                    raise HTTPException(403, i18n_module.tr("修改 Web 认证配置必须复验当前访问口令（reauth_password）"))
        try:
            validate_config_target(section, key, value)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        if is_web_section and key_norm == "web_password" and value.strip():
            normalized_password = value.strip()
            if not is_hashed_web_password(normalized_password):
                value = hash_web_password(normalized_password)
            else:
                value = normalized_password

        with state.file_update_lock:
            ok = update_config_line(cast(str, app.state.config_file), section, key, value)
        if not ok:
            raise HTTPException(404, i18n_module.tr("未找到对应的配置项"))
        if is_web_section and key_norm == "web_password" and req.value.strip():
            with _tokens_lock:
                _tokens.clear()
        if is_web_section and key_norm == "web_auth_enable":
            with _tokens_lock:
                _tokens.clear()
        if is_web_section and key_norm in (
            "web_auth_enable",
            "web_password",
            "web_host",
            "web_allowed_hosts",
            "web_trusted_proxy",
        ):
            _invalidate_web_cfg_cache()
        return {"ok": True}

    @_route(app, ["GET"], "/api/language")
    def get_language() -> dict[str, object]:
        from src.support import i18n as i18n_module

        return {
            "language": i18n_module.get_language(),
            "available": i18n_module.available_languages(),
        }

    @_route(app, ["PUT"], "/api/language")
    def set_language(req: LanguageUpdate) -> dict[str, object]:
        from src.support import i18n as i18n_module

        if not req.language.strip() or not i18n_module.is_recognized_language(req.language):
            raise HTTPException(400, i18n_module.tr("不支持的语言: {lang}", lang=req.language))
        normalized = i18n_module.normalize_language(req.language)
        previous = i18n_module.get_language()
        effective = i18n_module.set_language(normalized)

        with state.file_update_lock:
            written = update_or_append_config_line(cast(str, app.state.config_file), "录制设置", "language", effective)
        if not written:
            _ = i18n_module.set_language(previous)
            raise HTTPException(500, i18n_module.tr("语言配置写回失败"))
        return {
            "ok": True,
            "language": effective,
            "requested": normalized,
            "fallback": effective != normalized,
            "notice": (
                ""
                if effective == normalized
                else i18n_module.tr(
                    "{lang} 的翻译目录不可用，已回退为 {effective}（配置同样按 {effective} 写回）",
                    lang=normalized,
                    effective=effective,
                )
            ),
        }

    @_route(app, ["GET"], "/api/files")
    def list_files(path: str = "") -> list[dict[str, str | int | float]]:
        root = cast(str, app.state.downloads_root)
        target = os.path.realpath(os.path.join(root, path))
        if not _is_within(target, root):
            raise HTTPException(400, i18n_module.tr("非法路径"))
        if not os.path.exists(target):
            raise HTTPException(404, i18n_module.tr("路径不存在"))
        if os.path.isfile(target):
            st = os.stat(target)
            return [
                {
                    "name": os.path.basename(target),
                    "type": "file",
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "path": path,
                }
            ]
        items: list[dict[str, str | int | float]] = []
        for name in sorted(os.listdir(target)):
            full = os.path.join(target, name)
            resolved = os.path.realpath(full)
            if not _is_within(resolved, root):
                continue
            try:
                st = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, root).replace("\\", "/")
            items.append(
                {
                    "name": name,
                    "type": "dir" if os.path.isdir(full) else "file",
                    "size": st.st_size if os.path.isfile(full) else 0,
                    "mtime": st.st_mtime,
                    "path": rel,
                }
            )
        return items

    @_route(app, ["GET"], "/api/files/download")
    def download_file(path: str = "") -> FileResponse:
        root = cast(str, app.state.downloads_root)
        target = os.path.realpath(os.path.join(root, path))
        if not _is_within(target, root) or not os.path.isfile(target):
            raise HTTPException(400, i18n_module.tr("非法路径或文件不存在"))
        return FileResponse(target, filename=os.path.basename(target))

    @_route(app, ["GET"], "/api/logs")
    def get_logs(lines: int = 200) -> dict[str, list[str]]:
        log_file = os.path.join(cast(str, app.state.logs_dir), "streamget.log")
        if not os.path.isfile(log_file):
            return {"lines": cast(list[str], [])}
        try:
            with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                tail = deque(f, maxlen=lines)
            return {"lines": list(tail)}
        except Exception as e:
            _log_internal_error(_LOGS_ERROR_CODE, e)
            raise HTTPException(500, _LOGS_ERROR_CODE) from e

    @_route(app, ["GET"], "/api/danmaku")
    def get_danmaku(since: int = 0) -> dict[str, object]:
        try:
            from src.danmaku.monitor import get_hub

            return get_hub().snapshot(since)
        except Exception as e:
            _log_internal_error(_DANMAKU_ERROR_CODE, e, level="warning")
            return {"rooms": [], "messages": [], "last_seq": since, "truncated": False, "error": _DANMAKU_ERROR_CODE}

    if _WEB_DIR.exists():
        app.mount("/web", StaticFiles(directory=str(_WEB_DIR), html=True), name="web")

        @_route(app, ["GET"], "/")
        async def index() -> FileResponse:
            return FileResponse(str(_WEB_DIR / "index.html"))

    return app


def _purge_expired_tokens() -> None:
    now = time.time()
    with _tokens_lock:
        expired = [t for t, exp in _tokens.items() if exp <= now]
        for t in expired:
            _ = _tokens.pop(t, None)


def _status_worker(fetch_since: float) -> dict[str, object]:
    global _status_inflight, _status_inflight_since, _status_cache, _status_cache_at
    try:
        try:
            snapshot = recorder_status.get_status()
        except Exception as e:
            _log_internal_error(_STATUS_ERROR_CODE, e)
            return {"error": _STATUS_ERROR_CODE}
        with _status_lock:
            _status_cache = snapshot
            _status_cache_at = time.monotonic()
        return snapshot
    finally:
        with _status_lock:
            if _status_inflight and _status_inflight_since == fetch_since:
                _status_inflight = False


async def _get_status_snapshot() -> tuple[dict[str, object], bool]:
    global _status_inflight, _status_inflight_since
    now = time.monotonic()
    with _status_lock:
        cached, cached_at = _status_cache, _status_cache_at
        if cached is not None and (now - cached_at) < _STATUS_TTL_SECONDS:
            return cached, False
        inflight = _status_inflight and (now - _status_inflight_since) < _STATUS_INFLIGHT_LEASE
        if not inflight:
            _status_inflight = True
            _status_inflight_since = now
    fallback: dict[str, object] = cached if cached is not None else {"error": _STATUS_ERROR_CODE}
    if inflight:
        return fallback, True
    with _status_lock:
        fetch_since = _status_inflight_since
    try:
        snapshot = await asyncio.wait_for(asyncio.to_thread(_status_worker, fetch_since), _STATUS_WAIT_SECONDS)
        return snapshot, False
    except TimeoutError:
        return fallback, True
    except Exception as e:
        _log_internal_error(_STATUS_ERROR_CODE, e)
        return fallback, True


_web_cfg_cache: tuple[str, float, int, dict[str, str | int | bool]] = ("", -1.0, -1, {})


def _invalidate_web_cfg_cache() -> None:
    global _web_cfg_cache
    with _web_cfg_cache_lock:
        _web_cfg_cache = ("", -1.0, -1, {})


def _read_web_config_cached(config_file: str) -> dict[str, str | int | bool]:
    try:
        st = os.stat(config_file)
    except OSError:
        return read_web_config(config_file)
    global _web_cfg_cache
    with _web_cfg_cache_lock:
        path, mtime, size, cached = _web_cfg_cache
        if path == config_file and mtime == st.st_mtime and size == st.st_size:
            return cached
    cfg = read_web_config(config_file)
    with _web_cfg_cache_lock:
        _web_cfg_cache = (config_file, st.st_mtime, st.st_size, cfg)
    return cfg


def _is_same_origin(
    request: Request,
    cfg: dict[str, str | int | bool],
    bind_host: str,
    bind_port: int,
) -> bool:
    if request.headers.get("Sec-Fetch-Site", "").strip().lower() == "cross-site":
        return False
    origin = request.headers.get("Origin")
    if not origin:
        return True
    return is_origin_allowed(origin, cfg, bind_host=bind_host, bind_port=bind_port)


def _api_locked_out_by_insecure_bind(cfg: dict[str, str | int | bool], path: str, bind_host: str) -> bool:
    if not path.startswith("/api/"):
        return False
    if cast(bool, cfg["web_auth_enable"]) or _insecure_bind_allowed():
        return False
    return not is_loopback_bind_host(bind_host)


def _is_within(child: str, parent: str) -> bool:
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:
        return False
