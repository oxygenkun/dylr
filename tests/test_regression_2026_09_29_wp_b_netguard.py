"""使用真实请求钩子，验证每次重定向在发送前拒绝不安全目标。"""

from collections.abc import Callable

import httpx
import pytest
from loguru import logger

import src.network.http as async_http
from src.network import targets

# 与 tests/test_http.py / tests/test_regression_2026_09_22_net.py 同一个 DNS seam：MIN-2219
# 起探针会按「本机解析结果」给直连目标定罪，用例必须与该 seam 隔离，否则无 DNS 环境下「公网源可达」
# 类用例会随机转红。内网目标一律写成 IP 字面量——判定不依赖解析，也就不受本桩影响。
_PUBLIC_IP = "93.184.216.34"
# 代理环境变量键（大小写各一份，与 tests/test_proxy.py 的 _PROXY_ENV_KEYS 同族写法）
_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


@pytest.fixture(autouse=True)
def _hermetic_net(monkeypatch: pytest.MonkeyPatch) -> None:
    # ① DNS 钉成公网 IP。② NO_PROXY=* 而不只是逐个 delenv：httpx 的 get_environment_proxies() 走
    #    urllib.request.getproxies()，Windows 上 env 为空时会**回落到注册表**读系统代理；代理一旦挂进
    #    client._mounts，_transport_for_url 就不走 _transport，MockTransport 被静默绕过（纯假绿）。
    #    NO_PROXY=* 让 env 结果非空且 httpx 直接返回空代理表；逐个 delenv 同样保留。
    monkeypatch.setattr(targets, "resolve_host_ips", lambda host: [_PUBLIC_IP])
    for key in _PROXY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")


def _capture_logs() -> tuple[list[str], int]:
    # 返回 (日志行, handler id)；调用方必须 logger.remove(handler_id)（同 net 回归文件的口径）。
    lines: list[str] = []
    handler_id = logger.add(lambda message: lines.append(str(message)), level="DEBUG")
    return lines, handler_id


def _install_mock_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[str]:
    # 返回「实际发出的请求 URL 列表」（按跳顺序）。钩子是 _build_client 挂上去的，所以客户端必须由
    # **真实的 _build_client** 造出来，只把最底层 transport 换成 MockTransport——有人删掉 event_hooks
    # 那一行，本文件 M-1 的用例必须全部变红（自建 AsyncClient 会把被测接线一起绕过，等于假绿）。
    seen: list[str] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return handler(request)

    real_build = async_http._build_client

    def _build(proxy_addr: str | None, timeout: int, verify: bool, http2: bool) -> httpx.AsyncClient:
        client = real_build(proxy_addr, timeout, verify, http2)
        # typeshed 未声明、也无公开 setter。传了 proxy 时 httpx 会把代理传输装进 _mounts，
        # _transport_for_url 就不走 _transport；本文件只验判定链，故清空 mounts 让每跳落 MockTransport。
        client._mounts = {}
        client._transport = httpx.MockTransport(_record)
        return client

    monkeypatch.setattr(async_http, "_build_client", _build)
    return seen


def _redirect_chain(landing_target: str, *, after_landing: str = "") -> Callable[[httpx.Request], httpx.Response]:
    # 只造 httpx 的「3xx + Location」这一条行为，不做任何内网/协议判定（判定必须走生产代码）。
    # after_landing 用来验证「命中即停止跟随」：被拒之后绝不应再有下一跳。
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "edge.example.com":
            return httpx.Response(302, headers={"location": landing_target})
        if after_landing and request.url.host == "127.0.0.1":
            return httpx.Response(302, headers={"location": after_landing})
        # 落地跳回 200：没有逐跳防线时探针会据此判「可达」，回归因此可见
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="PONG")

    return _handler


class TestM1RedirectHopGuard:
    # M-1：逐跳复检（探针与 request 共用同一支钩子、同一个判定函数）.

    @pytest.mark.parametrize(
        "internal_target",
        [
            "http://127.0.0.1:6379/",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5/live.m3u8",
            "http://192.168.1.7/live.m3u8",
            "http://100.64.0.1/live.m3u8",
            "http://[::1]:6379/",
        ],
    )
    async def test_redirect_landing_on_internal_is_unreachable(
        self, internal_target: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 核心回归：初始 URL 是公网（过得了入口判定与 MIN-2219），跳转后落在内网/保留目标
        # → 必须判**不可达**，而不是拿该跳的 200 当结论。
        seen = _install_mock_transport(monkeypatch, _redirect_chain(internal_target))
        assert await async_http.is_reachable("https://edge.example.com/live.m3u8") is False
        assert seen == ["https://edge.example.com/live.m3u8"]

    async def test_rejected_hop_stops_the_redirect_chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 「命中即停止跟随」：被拒的那一跳之后不得再发第三跳，否则探针就成了 3xx 链上的跳板。
        seen = _install_mock_transport(
            monkeypatch,
            _redirect_chain("http://127.0.0.1:6379/", after_landing="https://after.example.com/x.m3u8"),
        )
        assert await async_http.is_reachable("https://edge.example.com/live.m3u8") is False
        assert "https://after.example.com/x.m3u8" not in seen, "被拒之后仍继续跟随重定向"

    async def test_hop_rejection_logs_warning_with_masked_landing_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 逐跳命中是**安全判定**，必须与 MIN-2219 初始地址同级别（warning）留线索，不能沉进「这个
        # CDN 节点不通」那条 debug；同时落地 URL 里的凭据必须被抹掉——httpx 把 Location 的 user:pass
        # 原样带进 response.url，而 logs 会轮转保留多份（＝凭据长期落盘）。
        lines, handler_id = _capture_logs()
        try:
            _install_mock_transport(monkeypatch, _redirect_chain("http://probe:s3cret@127.0.0.1:6379/"))
            assert await async_http.is_reachable("https://edge.example.com/live.m3u8") is False
        finally:
            logger.remove(handler_id)
        security_lines = [line for line in lines if "拒绝内网/保留目标" in line]
        assert len(security_lines) == 1, f"逐跳命中未按 warning 级安全判定落日志：{lines}"
        assert "WARNING" in security_lines[0]
        assert "重定向逐跳复检命中" in security_lines[0]
        assert "s3cret" not in security_lines[0], "落地 URL 的凭据进了日志"
        # 该跳响应的 content-type 不得作为「校验未通过」的细节被回显（内网端口观测口本体）
        assert not [line for line in lines if "text/plain" in line]

    async def test_public_to_public_redirect_still_reachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 反向锁（防「顺手把重定向全禁」的退化）：公网→公网的合法链路必须照旧判可达，
        # 且确实走完了两跳——把逐跳判定写成「任何重定向都拒」也会让本用例变红。
        def _handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).startswith("https://start.example.com"):
                return httpx.Response(302, headers={"location": "https://final.example.com/live.m3u8"})
            return httpx.Response(200, headers={"content-type": "application/vnd.apple.mpegurl"})

        seen = _install_mock_transport(monkeypatch, _handler)
        assert await async_http.is_reachable("https://start.example.com/live.m3u8") is True
        assert seen == ["https://start.example.com/live.m3u8", "https://final.example.com/live.m3u8"]

    async def test_async_req_redirect_to_internal_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 同一支钩子也必须覆盖 request（M-1 里「各分支同样全线跟随重定向」那半）。
        # 返回契约不变：与网络异常同一条失败出口（空串），调用方按「本轮没拿到数据」处理。
        seen = _install_mock_transport(monkeypatch, _redirect_chain("http://127.0.0.1:6379/"))
        assert await async_http.request("https://edge.example.com/api") == ""
        assert seen == ["https://edge.example.com/api"]

    async def test_async_req_post_branch_is_covered_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # POST(json=) 分支同样 follow_redirects=True。判定只有一处定义点、四条分支共用一支
        # 客户端 → 不必在每个分支各挂一次；漏挂的分支会安静地把内网响应当成业务数据返回。
        seen = _install_mock_transport(monkeypatch, _redirect_chain("http://169.254.169.254/"))
        assert await async_http.request("https://edge.example.com/gql", json_data={"a": 1}) == ""
        assert len(seen) == 1

    async def test_stateful_client_carries_the_guard_as_well(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # return_cookies=True 走 _acquire_client 的独占分支（每次自建、不进缓存），那条分支同样调
        # _build_client → 钩子必须在。漏掉它等于给登录/取 token 路径留一个无人看守的重定向面。
        seen = _install_mock_transport(monkeypatch, _redirect_chain("http://127.0.0.1:6379/"))
        assert await async_http.request("https://edge.example.com/login", return_cookies=True) == {}
        assert len(seen) == 1

    async def test_guard_is_installed_on_every_built_client(self) -> None:
        # 接线点结构锁：钩子出自 _build_client（探针与 request 唯一的客户端来源）。这条与既有
        # 「单次选源复用 / 不做模块级全局 client 缓存 / 不关 keepalive / _get_client_lock 随循环重建」
        # 等约束互不冲突——只钉「每支构造出来的客户端都带着一个 response 钩子」。
        client = async_http._build_client(None, 10, True, False)
        try:
            assert len(client.event_hooks["response"]) == 1
            # URL 在 request 钩子校验，response 钩子只负责有界读取。
            assert len(client.event_hooks["request"]) == 1
        finally:
            await client.aclose()

    async def test_proxied_chain_still_judges_ip_literal_landing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 经代理出站时域名不按本机 DNS 定罪（MIN-2219 的既有豁免，逐跳复检必须继承，否则会把
        # 「境外域名本机被污染、代理却能解析」的合法流误杀）；但落地是 IP 字面量时两种情况都判。
        dns_calls: list[str] = []

        def _record_dns(host: str) -> list[str]:
            dns_calls.append(host)
            return [_PUBLIC_IP]

        monkeypatch.setattr(targets, "resolve_host_ips", _record_dns)
        seen = _install_mock_transport(monkeypatch, _redirect_chain("http://127.0.0.1:6379/"))
        assert (
            await async_http.is_reachable("https://edge.example.com/live.m3u8", proxy_addr="http://127.0.0.1:1")
            is False
        )
        assert len(seen) == 1
        # 豁免仍然成立：公网域名那一跳没有按本机解析定罪（初始终址与中间跳都不查 DNS）
        assert dns_calls == []
