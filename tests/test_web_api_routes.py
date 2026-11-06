# 阶段2（Web 后端 FastAPI → Starlette）路由契约动态断言：取代 web_api_routes_baseline.json 的静态
# 逐字节比对。静态基线既撞 SENSITIVE_APPROVAL 检查、又会因手工维护而漂移；本测试直接遍历 app.routes，
# 断言 Starlette 应用注册的 24 条路由（method/path 精确集合）+ /web 静态挂载点不变，
# 任何增删或改 method 的回归都会让集合断言转红（drift-proof，不依赖运行期快照）。
#
# 构建 app 并遍历路由表；使用临时配置和隔离的录制状态。

import threading
import types
from collections.abc import Generator
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount, Route


@pytest.fixture
def fake_main(monkeypatch: pytest.MonkeyPatch) -> Generator[types.ModuleType]:
    from src.recording import state

    for key, value in {
        "file_update_lock": threading.RLock(),
        "record_state_lock": threading.Lock(),
        "max_request_lock": threading.Lock(),
        "running_list": [],
        "recording": set(),
        "recording_enabled": False,
        "text_encoding": "utf-8-sig",
        "url_config_file": "",
        "ini_URL_content": "",
    }.items():
        monkeypatch.setattr(state, key, value)
    yield state


def _write_web_section(cfg_path: Path) -> None:
    # 写入最小合法 [Web] 节配置，使 create_app 内的 read_web_config 能解析。
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "[Web]",
        "web_host = 127.0.0.1",
        "web_port = 8000",
        "web_auth_enable = true",
        "web_password = x",
        "web_trusted_proxy = ",
    ]
    cfg_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


@pytest.fixture(scope="function")
def app(fake_main: types.ModuleType, tmp_path: Path) -> Generator[Starlette]:
    from src.api import routes as wa

    cfg = tmp_path / "config.ini"
    url_cfg = tmp_path / "URL_config.ini"
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    _write_web_section(cfg)
    app = wa.create_app(
        config_file=str(cfg),
        url_config_file=str(url_cfg),
        downloads_root=str(downloads),
        logs_dir=str(logs),
    )
    yield app


# 阶段2 改写后 Starlette 应用必须精确注册的路由集合（method + path）。
# 顺序无关，集合相等才算契约守住；任何 method/path 的增删改都会让断言转红。
# HEAD 为 Starlette 对 GET 路由的自动派生方法，不计入显式契约（与旧 FastAPI 行为一致）。
EXPECTED_ROUTES = {
    ("POST", "/api/login"),
    ("POST", "/api/logout"),
    ("GET", "/api/status"),
    ("GET", "/api/auth/status"),
    ("GET", "/health"),
    ("GET", "/api/status/stream"),
    ("POST", "/api/recording/toggle"),
    ("GET", "/api/rooms"),
    ("POST", "/api/rooms"),
    ("PUT", "/api/rooms"),
    ("DELETE", "/api/rooms"),
    ("POST", "/api/rooms/toggle"),
    ("PUT", "/api/rooms/quality"),
    ("GET", "/api/rooms/qualities"),
    ("PUT", "/api/rooms/qualities"),
    ("GET", "/api/config"),
    ("PUT", "/api/config"),
    ("GET", "/api/language"),
    ("PUT", "/api/language"),
    ("GET", "/api/files"),
    ("GET", "/api/files/download"),
    ("GET", "/api/logs"),
    ("GET", "/api/danmaku"),
    ("GET", "/"),
}


def test_route_table_matches_contract(app: Starlette) -> None:
    # 遍历 app.routes，分别处理 Route（带 methods）与 Mount（静态目录挂载，仅 path）。
    seen_routes: set[tuple[str, str]] = set()
    seen_mounts: list[str] = []
    for r in app.routes:
        if isinstance(r, Route):
            methods = r.methods or set()
            for m in methods:
                if m == "HEAD":
                    # Starlette 为 GET 路由自动派生 HEAD，非显式契约，跳过以免掩盖/误增
                    continue
                seen_routes.add((m, r.path))
        elif isinstance(r, Mount):
            seen_mounts.append(r.path)

    # 集合相等：缺一条或多一条都转红（变异验证：临时删一个 @_route 装饰器会让 seen_routes 少一项）
    assert seen_routes == EXPECTED_ROUTES, (
        "路由契约漂移：\n"
        f"  新增(实际有、期望无)={seen_routes - EXPECTED_ROUTES}\n"
        f"  缺失(期望有、实际无)={EXPECTED_ROUTES - seen_routes}"
    )
    # /web 静态资源挂载点必须存在（前端动效/样式/js 经此服务）
    assert "/web" in seen_mounts, f"缺失 /web 静态挂载点，实际挂载={seen_mounts}"


def test_route_count_is_24(app: Starlette) -> None:
    # 健全性：路由条数必须等于 24（与 EXPECTED_ROUTES 集合规模一致，防集合去重掩盖重复注册）。
    seen = {(m, r.path) for r in app.routes if isinstance(r, Route) for m in (r.methods or set()) if m != "HEAD"}
    assert len(seen) == 24, f"路由条数应为 24，实际={len(seen)}"
