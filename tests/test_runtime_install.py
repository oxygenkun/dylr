"""Direct runtime installation without network or package-manager side effects."""

import copy
import hashlib
import io
import os
import stat
import subprocess
import tarfile
import types
import zipfile
from pathlib import Path

import pytest

from src import runtime_install as runtime


@pytest.fixture(autouse=True)
def isolated_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "RESOURCES", copy.deepcopy(runtime.RESOURCES))


def archive_bytes(members: dict[str, bytes], *, zipped: bool = True) -> bytes:
    output = io.BytesIO()
    if zipped:
        with zipfile.ZipFile(output, "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)
    else:
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            for name, data in members.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))
    return output.getvalue()


@pytest.mark.parametrize("name", ["ffmpeg", "node"])
@pytest.mark.parametrize(
    ("system", "machine", "key", "node_platform"),
    [
        ("win32", "AMD64", "windows-x64", "win"),
        ("win32", "ARM64", "windows-x64", "win"),
        ("linux", "x86_64", "linux-x64", "linux"),
        ("linux", "aarch64", "linux-arm64", "linux"),
        ("darwin", "x86_64", "macos-x64", "darwin"),
        ("darwin", "arm64", "macos-arm64", "darwin"),
    ],
)
def test_configured_archives_install_required_binaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    system: str,
    machine: str,
    key: str,
    node_platform: str,
) -> None:
    monkeypatch.setattr(runtime, "sys", types.SimpleNamespace(platform=system))
    monkeypatch.setattr(runtime, "platform", types.SimpleNamespace(machine=lambda: machine))
    suffix = ".exe" if system == "win32" else ""
    binaries = (f"{name}{suffix}",)
    payloads = {}
    if name == "node":
        runtime.RESOURCES["node"]["version"] = "v99.0.0"
        extension = "zip" if suffix else "tar.gz"
        url = f"https://nodejs.org/dist/v99.0.0/node-v99.0.0-{node_platform}-{key.split('-')[1]}.{extension}"
        payloads[url] = archive_bytes({f"package/bin/{binaries[0]}": b"node"}, zipped=bool(suffix))
        runtime.RESOURCES["pins"][key]["node"] = hashlib.sha256(payloads[url]).hexdigest()
    else:
        url = runtime.RESOURCES["ffmpeg"][key]
        payloads[url] = archive_bytes({f"bin/{binaries[0]}": binaries[0].encode()}, zipped=system != "linux")
        if system != "darwin":
            runtime.RESOURCES["pins"][key]["ffmpeg"] = hashlib.sha256(payloads[url]).hexdigest()
    seen = []
    verified = []

    def download(url: str, target: Path) -> None:
        seen.append(url)
        target.write_bytes(payloads[url])

    monkeypatch.setattr(runtime, "_download", download)
    monkeypatch.setattr(
        runtime, "_verify_signature", lambda archive, url, fingerprint: verified.append((url, fingerprint))
    )
    runtime.install_runtime(name, tmp_path)
    assert set(seen) == set(payloads)
    assert {item.name for item in (tmp_path / name).iterdir()} == set(binaries)
    assert all((tmp_path / name / binary).read_bytes() for binary in binaries)
    assert {item.name for item in tmp_path.iterdir()} == {name}
    if name == "ffmpeg" and system == "darwin":
        assert verified == [tuple(entry) for entry in runtime.RESOURCES["signatures"][key].values()]


@pytest.mark.parametrize("failure", ["hash", "missing", "network", "unpinned"])
def test_failed_install_preserves_existing_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    monkeypatch.setattr(runtime, "runtime_key", lambda: "windows-x64")
    target = tmp_path / "ffmpeg"
    target.mkdir()
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        (target / name).write_bytes(b"old")
    payload = archive_bytes({"README" if failure == "missing" else "ffmpeg.exe": b"new"})
    runtime.RESOURCES["pins"]["windows-x64"]["ffmpeg"] = (
        "unverified"
        if failure == "unpinned"
        else "0" * 64
        if failure == "hash"
        else hashlib.sha256(payload).hexdigest()
    )
    downloads = []

    def download(url: str, path: Path) -> None:
        downloads.append(url)
        if failure in ("unpinned", "network"):
            raise OSError("No archive should be installed")
        path.write_bytes(payload)

    monkeypatch.setattr(runtime, "_download", download)
    with pytest.raises((ValueError, OSError)):
        runtime.install_runtime("ffmpeg", tmp_path)
    assert [(target / name).read_bytes() for name in ("ffmpeg.exe", "ffprobe.exe")] == [b"old", b"old"]
    assert list(tmp_path.iterdir()) == [target]
    if failure == "unpinned":
        assert downloads == []


def test_archive_paths_never_escape_destination(tmp_path: Path) -> None:
    archive = tmp_path / "runtime.zip"
    archive.write_bytes(archive_bytes({"../../node": b"node", "../../other": b"other"}))
    output = tmp_path / "stage" / "out"
    output.mkdir(parents=True)
    runtime._extract(archive, output / "node")
    assert {item.name for item in tmp_path.iterdir()} == {"runtime.zip", "stage"}
    assert (output / "node").read_bytes() == b"node"
    assert not (tmp_path / "other").exists()


@pytest.mark.parametrize("zipped", [True, False])
def test_archive_links_are_rejected(tmp_path: Path, zipped: bool) -> None:
    archive = tmp_path / "runtime.archive"
    if zipped:
        with zipfile.ZipFile(archive, "w") as source:
            link = zipfile.ZipInfo("node")
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            source.writestr(link, "/outside")
    else:
        with tarfile.open(archive, "w") as source:
            link = tarfile.TarInfo("node")
            link.type = tarfile.SYMTYPE
            link.linkname = "/outside"
            source.addfile(link)
    output = tmp_path / "out"
    output.mkdir()
    with pytest.raises(ValueError):
        runtime._extract(archive, output / "node")


@pytest.mark.parametrize(
    ("requested", "redirected"),
    [
        ("http://example.test/a", "https://example.test/a"),
        ("https://example.test/a", "http://example.test/a"),
        ("https://example.test/a", "https://example.test/b"),
    ],
)
def test_download_requires_https_before_and_after_redirect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requested: str,
    redirected: str,
) -> None:
    class Response(io.BytesIO):
        def geturl(self) -> str:
            return redirected

    request = types.SimpleNamespace(urlopen=lambda url, timeout: Response(b"archive"))
    monkeypatch.setattr(runtime, "urllib", types.SimpleNamespace(request=request))
    archive = tmp_path / "archive"
    if requested.startswith("http:") or redirected.startswith("http:"):
        with pytest.raises(ValueError):
            runtime._download(requested, archive)
    else:
        runtime._download(requested, archive)
        assert archive.read_bytes() == b"archive"


@pytest.mark.parametrize("signer, bad", [("a" * 40, False), ("b" * 40, False), ("a" * 40, True)])
def test_signature_requires_pinned_primary_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signer: str,
    bad: bool,
) -> None:
    monkeypatch.setattr(runtime, "_download", lambda url, path: path.write_bytes(b"signature"))
    commands = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert kwargs["check"] is True
        commands.append(command)
        status = f"[GNUPG:] GOODSIG key signer\n[GNUPG:] VALIDSIG {'c' * 40} 0 0 0 0 0 0 0 0 {signer}\n".encode()
        if bad:
            status += b"[GNUPG:] BADSIG key signer\n"
        return subprocess.CompletedProcess(command, 0, status, b"")

    shim = types.SimpleNamespace(**vars(subprocess))
    shim.run = run
    monkeypatch.setattr(runtime, "subprocess", shim)
    if signer == "a" * 40 and not bad:
        runtime._verify_signature(tmp_path / "archive", "https://example.test/file.sig", "a" * 40)
    else:
        with pytest.raises(ValueError):
            runtime._verify_signature(tmp_path / "archive", "https://example.test/file.sig", "a" * 40)
    assert all("--homedir" in command for command in commands)


@pytest.mark.parametrize(
    "available, disabled, succeeds", [(True, False, True), (False, True, False), (False, False, True)]
)
def test_ensure_runtime_uses_existing_or_direct_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    available: bool,
    disabled: bool,
    succeeds: bool,
) -> None:
    installed = []
    monkeypatch.setenv("PATH", "system-bin")
    monkeypatch.setenv("DOUYIN_SKIP_RUNTIME_CHECK", str(disabled))
    shim = types.SimpleNamespace(**vars(subprocess))
    shim.run = lambda command, **kwargs: subprocess.CompletedProcess(
        command, 0 if available or installed else 1, b"v1", b""
    )
    monkeypatch.setattr(runtime, "subprocess", shim)
    monkeypatch.setattr(runtime, "install_runtime", lambda name, root: installed.append((name, root)))
    assert runtime.ensure_runtime("node", str(tmp_path)) is succeeds
    assert installed == ([] if available or disabled else [("node", tmp_path)])
    assert os.environ["PATH"] == ("system-bin" if not installed else str(tmp_path / "node") + os.pathsep + "system-bin")


def test_failed_download_returns_false_without_changing_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "system-bin")
    monkeypatch.delenv("DOUYIN_SKIP_RUNTIME_CHECK", raising=False)
    shim = types.SimpleNamespace(**vars(subprocess))
    shim.run = lambda command, **kwargs: subprocess.CompletedProcess(command, 1, b"", b"")
    monkeypatch.setattr(runtime, "subprocess", shim)

    def fail(name: str, root: Path) -> None:
        raise OSError("download unavailable")

    monkeypatch.setattr(runtime, "install_runtime", fail)
    assert runtime.ensure_runtime("node", str(tmp_path)) is False
    assert os.environ["PATH"] == "system-bin"
