# The imported signing implementation must match the old deployed source exactly.
# Its deterministic output is verified using a frozen clock and synthetic request data.
# The signature golden was computed from the untouched ihmily local-source implementation.
# SM3 reference digests were independently checked with OpenSSL through hashlib.new('sm3').
# Literal vectors keep these tests portable to Python builds without OpenSSL SM3 support.
# Empty, short and full-block inputs exercise the hash padding boundaries.
# No authenticated cookies or actual account URLs are used by these tests.
# A local module time shim prevents the frozen clock from affecting pytest or logging.
# Exercising the complete signer also covers the custom alphabet and RC4 packing steps.
# The signature is a compatibility fixture, not a production request or a reusable credential.
import time
import types

import pytest

from src.streams import ab_sign as signer


@pytest.mark.parametrize(
    ("text", "digest"),
    [
        ("", "1ab21d8355cfa17f8e61194831e81a8f22bec8c728fefb747ed035eb5082aa2b"),
        ("abc", "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0"),
        ("a" * 64, "616ec433c359e7c2b19f360e2b8f2a1b6e9ed76b8dc1a7d207b31a5341c611e9"),
    ],
)
def test_sm3_reference_vectors(text: str, digest: str) -> None:
    assert signer.SM3().sum(text, "hex") == digest


def test_signer_matches_original_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = types.SimpleNamespace(**vars(time))
    clock.time = lambda: 1700000000.25
    monkeypatch.setattr(signer, "time", clock)
    expected = (
        "E7mhBmg6mEVNgf6X56KLfY3q64H3Y5/I0HViMD2fcxVEqL39HMPc9exoIBGvXtyjwG/-IeYjy4hbO3xprQAjM36UHWwEUdQ2m"
        "gWkKl5Q5I0j53iruyRDntmF4vj3SFlm5XNAEOk0y75rKb70Woqe-vIlO62-zo0/9Xm="
    )
    assert signer.ab_sign("aid=6383&count=50", "test-agent") == expected
