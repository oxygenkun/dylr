import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from src.support import i18n
from src.support.logger import logger

PYCACHE_DIR_NAME = "__pycache__"

BYTECODE_SUFFIXES = (".pyc", ".pyo")

STAMP_DIR_NAME = "logs"
STAMP_FILE_NAME = ".pyc_stamp"

_STAMP_SEPARATOR = "|"

_IS_FROZEN = bool(getattr(sys, "frozen", False))


@dataclass(frozen=True)
class PurgeResult:
    triggered: bool
    removed_dirs: int
    freed_bytes: int
    errors: int


def _stamp_path(app_root: Path) -> Path:
    return app_root / STAMP_DIR_NAME / STAMP_FILE_NAME


def _scan_dirs(app_root: Path) -> list[Path]:
    dirs: list[Path] = []
    if any(app_root.glob("*.py")):
        dirs.append(app_root)
    src_root = app_root / "src"
    if src_root.is_dir():
        for root, subdirs, files in os.walk(src_root):
            if any(name.endswith(".py") for name in files):
                dirs.append(Path(root))
            subdirs[:] = [name for name in subdirs if name != PYCACHE_DIR_NAME]
    return dirs


def _tree_fingerprint(app_root: Path, scan_dirs: list[Path]) -> str:
    hasher = hashlib.sha256()
    for directory in scan_dirs:
        for source in sorted(directory.glob("*.py")):
            hasher.update(f"{source.relative_to(app_root).as_posix()}\n".encode("utf-8"))
            hasher.update(source.read_bytes())
    return hasher.hexdigest()


def _read_stamp(app_root: Path) -> tuple[str, str] | None:
    try:
        text = _stamp_path(app_root).read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
        return None
    version, separator, digest = text.strip().partition(_STAMP_SEPARATOR)
    if not separator or not version or not digest:
        return None
    return version, digest


def _write_stamp(app_root: Path, version: str, digest: str) -> bool:
    target = _stamp_path(app_root)
    temp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(f"{version}{_STAMP_SEPARATOR}{digest}", encoding="utf-8")
        os.replace(temp, target)
    except OSError as e:
        logger.warning(
            i18n.tr(
                "字节码缓存基线写入失败(下轮仍会清理): {type_name}: {err}",
                type_name=type(e).__name__,
                err=e,
            )
        )
        try:
            temp.unlink()
        except OSError:
            pass
        return False
    return True


def _purge_cache_dir(cache: Path) -> tuple[int, int, int]:
    freed_bytes = 0
    errors = 0
    for entry in sorted(cache.glob("*")):
        if entry.suffix not in BYTECODE_SUFFIXES:
            continue
        try:
            if entry.is_file():
                freed_bytes += entry.stat().st_size
            entry.unlink()
        except OSError as e:
            errors += 1
            logger.warning(
                i18n.tr(
                    "字节码缓存文件删除失败(跳过): {name} - {type_name}: {err}",
                    name=entry.name,
                    type_name=type(e).__name__,
                    err=e,
                )
            )
    removed = 0
    try:
        cache.rmdir()
        removed = 1
    except OSError as e:
        errors += 1
        logger.warning(
            i18n.tr(
                "字节码缓存目录未删净(跳过): {name} - {type_name}: {err}",
                name=cache.name,
                type_name=type(e).__name__,
                err=e,
            )
        )
    return removed, freed_bytes, errors


def _purge(root: Path, version: str) -> PurgeResult:
    scan_dirs = _scan_dirs(root)
    digest = _tree_fingerprint(root, scan_dirs)
    baseline = _read_stamp(root)
    if baseline == (version, digest):
        return PurgeResult(triggered=False, removed_dirs=0, freed_bytes=0, errors=0)

    removed_dirs = 0
    freed_bytes = 0
    errors = 0
    for directory in scan_dirs:
        cache = directory / PYCACHE_DIR_NAME
        if not cache.is_dir():
            continue
        one_removed, one_freed, one_errors = _purge_cache_dir(cache)
        removed_dirs += one_removed
        freed_bytes += one_freed
        errors += one_errors

    stamp_written = _write_stamp(root, version, digest)
    if not stamp_written:
        errors += 1

    if removed_dirs:
        logger.info(
            i18n.tr(
                "源码或版本已变化，已清理 {count} 个字节码缓存目录（释放 {freed_kb} KB）",
                count=removed_dirs,
                freed_kb=freed_bytes // 1024,
            )
        )
    return PurgeResult(triggered=True, removed_dirs=removed_dirs, freed_bytes=freed_bytes, errors=errors)


def purge_stale_bytecode_caches(app_root: str | Path, version: str, enabled: bool = True) -> PurgeResult:
    if not enabled or _IS_FROZEN:
        return PurgeResult(triggered=False, removed_dirs=0, freed_bytes=0, errors=0)
    try:
        return _purge(Path(app_root), version)
    except (OSError, UnicodeDecodeError) as e:
        logger.warning(
            i18n.tr(
                "字节码缓存清理未完成(忽略): {type_name}: {err}",
                type_name=type(e).__name__,
                err=e,
            )
        )
        return PurgeResult(triggered=False, removed_dirs=0, freed_bytes=0, errors=1)
