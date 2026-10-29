"""控制面与拉流侧的 TLS 验证策略。"""

verify_tls: bool = True

verify_stream_tls: bool = True

stream_tls_overrides: dict[str, bool] = {}


def should_verify_stream_tls(platform: str | None = None) -> bool:
    if verify_stream_tls and platform:
        return stream_tls_overrides.get(platform, True)
    return verify_stream_tls
