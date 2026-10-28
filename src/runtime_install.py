"""Direct downloads shared by application startup and release builds."""

import hashlib
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from src.external_resources import RESOURCES


def runtime_key() -> str:
    system = {"win32": "windows", "linux": "linux", "darwin": "macos"}[sys.platform]
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}[platform.machine().lower()]
    key = f"{system}-{arch}"
    return "windows-x64" if key == "windows-arm64" else key


def _download(url: str, target: Path) -> None:
    if urlsplit(url).scheme != "https":
        raise ValueError("Runtime downloads require HTTPS")
    with urllib.request.urlopen(url, timeout=60) as response, target.open("wb") as output:
        if urlsplit(response.geturl()).scheme != "https":
            raise ValueError("Runtime download redirected outside HTTPS")
        shutil.copyfileobj(response, output, 1024 * 1024)


def _verify_signature(archive: Path, url: str, fingerprint: str) -> None:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", fingerprint):
        raise ValueError("Missing pinned GPG fingerprint")
    signature = archive.with_suffix(archive.suffix + ".sig")
    _download(url, signature)
    # A private keyring avoids modifying the user's GPG configuration or trust store.
    with tempfile.TemporaryDirectory() as keyring:
        command = ["gpg", "--homedir", keyring, "--batch"]
        subprocess.run(
            [*command, "--keyserver", RESOURCES["gpg_keyserver"], "--recv-keys", fingerprint],
            capture_output=True,
            timeout=60,
            check=True,
        )
        result = subprocess.run(
            [*command, "--status-fd", "1", "--verify", str(signature), str(archive)],
            capture_output=True,
            timeout=30,
            check=True,
        )
        # VALIDSIG ends with the primary fingerprint when a signing subkey is used.
        lines = result.stdout.splitlines()
        valid = [line.split() for line in lines if line.startswith(b"[GNUPG:] VALIDSIG ")]
        expected = fingerprint.lower().encode("ascii")
        good = any(line.startswith(b"[GNUPG:] GOODSIG ") for line in lines)
        bad = any(line.startswith((b"[GNUPG:] BADSIG ", b"[GNUPG:] ERRSIG ")) for line in lines)
        if (
            not good
            or bad
            or not valid
            or any(len(fields) < 3 or expected not in (fields[2].lower(), fields[-1].lower()) for fields in valid)
        ):
            raise ValueError("Runtime signature does not match the pinned key")


def _extract(archive: Path, target: Path) -> None:
    # Copy one regular executable to a controlled path; never extract archive paths.
    name = target.name
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as source:
            entries = [item for item in source.infolist() if Path(item.filename).name == name and not item.is_dir()]
            if len(entries) != 1:
                raise ValueError(f"Archive must contain exactly one {name}")
            entry = entries[0]
            if stat.S_ISLNK(entry.external_attr >> 16) or not 0 < entry.file_size <= 4 * 1024**3:
                raise ValueError(f"Invalid executable member: {name}")
            if entry.file_size > 10 * 1024**2 and entry.file_size > max(entry.compress_size, 1) * 100:
                raise ValueError(f"Excessive compression ratio: {name}")
            with source.open(entry) as data, target.open("wb") as output:
                shutil.copyfileobj(data, output, 1024 * 1024)
    else:
        with tarfile.open(archive) as source:
            entries = [item for item in source.getmembers() if Path(item.name).name == name]
            if len(entries) != 1 or not entries[0].isfile() or not 0 < entries[0].size <= 4 * 1024**3:
                raise ValueError(f"Archive must contain exactly one regular {name}")
            data = source.extractfile(entries[0])
            if data is None:
                raise ValueError(f"Missing executable data: {name}")
            with data, target.open("wb") as output:
                shutil.copyfileobj(data, output, 1024 * 1024)
    target.chmod(0o755)


def install_runtime(name: str, root: Path) -> None:
    key = runtime_key()
    suffix = ".exe" if key.startswith("windows-") else ""
    if name == "node":
        node = RESOURCES["node"]
        system, arch = key.split("-")
        system = {"windows": "win", "macos": "darwin", "linux": "linux"}[system]
        extension = "zip" if suffix else "tar.gz"
        url = f"{node['dist']}{node['version']}/node-{node['version']}-{system}-{arch}.{extension}"
    elif name == "ffmpeg":
        url = RESOURCES["ffmpeg"][key]
    else:
        raise ValueError(f"Unknown runtime: {name}")
    expected = RESOURCES["pins"][key][name]
    signature = RESOURCES["signatures"].get(key, {}).get(name)
    if not re.fullmatch(r"[0-9a-f]{64}", expected) and not (expected == "PINNED-OFFICIAL-GPG-SIGNATURE" and signature):
        raise ValueError(f"Missing integrity pin: {key}/{name}")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as temporary:
        work = Path(temporary)
        archive = work / "archive"
        _download(url, archive)
        if expected == "PINNED-OFFICIAL-GPG-SIGNATURE":
            _verify_signature(archive, *signature)
        else:
            with archive.open("rb") as data:
                actual = hashlib.file_digest(data, "sha256").hexdigest()
            if actual != expected:
                raise ValueError(f"SHA256 mismatch: {key}/{name}")
        binary = work / f"{name}{suffix}"
        _extract(archive, binary)
        # Preserve the existing executable until the download and extraction both succeed.
        destination = root / name
        destination.mkdir(exist_ok=True)
        os.replace(binary, destination / binary.name)


def ensure_runtime(name: str, root: str) -> bool:
    from loguru import logger

    from src import i18n
    from src.config_bool import parse_config_bool

    flag = "-v" if name == "node" else "-version"

    def available() -> bool:
        try:
            result = subprocess.run(
                [name, flag],
                capture_output=True,
                timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            return result.returncode == 0 and bool(result.stdout.strip())
        except OSError, subprocess.TimeoutExpired:
            return False

    if available():
        return True
    if parse_config_bool(os.environ.get("DOUYIN_SKIP_RUNTIME_CHECK"), False):
        return False
    try:
        install_runtime(name, Path(root))
        os.environ["PATH"] = str(Path(root) / name) + os.pathsep + os.environ.get("PATH", "")
        if not available():
            raise RuntimeError("Downloaded executable cannot run")
        return True
    except Exception as error:
        logger.error(i18n.tr("运行时 {name} 下载或安装失败，请手动安装: {error}", name=name, error=error))
        return False
