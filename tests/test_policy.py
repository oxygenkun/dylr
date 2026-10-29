"""SSL 控制面与拉流策略，以及平台豁免的配置应用。"""

import configparser

import pytest

from src.network import policy
from src.recording import engine


@pytest.mark.parametrize("control_verify", [True, False])
@pytest.mark.parametrize(
    ("stream_verify", "overrides", "platform", "expected"),
    [
        (True, {}, None, True),
        (True, {"虎牙直播": False}, "", True),
        (True, {"虎牙直播": False}, "抖音直播", True),
        (True, {"虎牙直播": False}, "虎牙直播", False),
        (True, {"虎牙直播": True}, "虎牙直播", True),
        (False, {}, None, False),
        (False, {"虎牙直播": False}, "虎牙直播", False),
        (False, {"虎牙直播": True}, "虎牙直播", False),
    ],
)
def test_effective_ssl_verify(
    monkeypatch: pytest.MonkeyPatch,
    control_verify: bool,
    stream_verify: bool,
    overrides: dict[str, bool],
    platform: str | None,
    expected: bool,
) -> None:
    monkeypatch.setattr(policy, "verify_tls", control_verify)
    monkeypatch.setattr(policy, "verify_stream_tls", stream_verify)
    monkeypatch.setattr(policy, "stream_tls_overrides", overrides)

    assert policy.should_verify_stream_tls(platform) is expected
    assert policy.verify_tls is control_verify


def test_apply_platform_exemptions_keeps_existing_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(policy, "stream_tls_overrides", {"已有平台": False, "其他平台": True})
    config = configparser.RawConfigParser()
    config.read_dict({"录制设置": {"禁用SSL证书验证的平台(逗号分隔)": "虎牙直播， B站直播, 抖音直播,,"}})

    assert engine._apply_ssl_platform_exempt(config) == {"虎牙直播", "B站直播", "抖音直播"}
    assert policy.stream_tls_overrides == {
        "已有平台": False,
        "其他平台": True,
        "虎牙直播": False,
        "B站直播": False,
        "抖音直播": False,
    }

    config.set("录制设置", "禁用SSL证书验证的平台(逗号分隔)", "虎牙直播,B站直播")
    engine._apply_ssl_platform_exempt(config)
    assert policy.stream_tls_overrides["抖音直播"] is False
    assert policy.stream_tls_overrides["其他平台"] is True
