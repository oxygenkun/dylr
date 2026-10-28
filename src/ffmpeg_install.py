"""FFmpeg startup checks and bundled executable preference."""

import os
import platform
import shutil
import sys

from loguru import logger

from src import i18n, utils
from src.logger import script_path
from src.runtime_install import ensure_runtime

ffmpeg_path = os.path.join(script_path, "ffmpeg")


def check_ffmpeg() -> bool:
    return ensure_runtime("ffmpeg", script_path)


def should_prepend_bundled_ffmpeg_dir(bundled_dir: str, current_path: str) -> bool:
    # Prefer an existing native installation over bundled x64 binaries on Apple Silicon.
    if sys.platform != "darwin":
        return True

    if platform.machine().lower() != "arm64":
        return True

    if not os.path.isdir(bundled_dir):
        return True

    system_ffmpeg = shutil.which("ffmpeg", path=current_path) if current_path else None

    if system_ffmpeg:
        candidate_real = os.path.realpath(system_ffmpeg)
        bundle_real = os.path.realpath(bundled_dir)
        if candidate_real == bundle_real or candidate_real.startswith(bundle_real + os.sep):
            logger.debug(
                i18n.tr(
                    "FFmpeg PATH 优先级：Apple Silicon 上系统 PATH 探测到的 {system_ffmpeg} "
                    "就是包内目录自身，仍前置包内目录 {bundled_dir}（没有原生 arm64 构建可让位）",
                    system_ffmpeg=utils.mask_credentials(system_ffmpeg),
                    bundled_dir=utils.mask_credentials(bundle_real),
                )
            )
            return True
    else:
        logger.debug(
            i18n.tr(
                "FFmpeg PATH 优先级：Apple Silicon 上系统 PATH 未探测到 ffmpeg，"
                "仍前置包内目录 {bundled_dir}（包内为 x86_64 构建，需经 Rosetta 运行）",
                bundled_dir=utils.mask_credentials(os.path.realpath(bundled_dir)),
            )
        )
        return True

    logger.debug(
        i18n.tr(
            "FFmpeg PATH 优先级：让位于系统原生 ffmpeg {system_ffmpeg}，不再前置包内目录 "
            "{bundled_dir}（包内为 x86_64 构建，Apple Silicon 需经 Rosetta 转译）",
            system_ffmpeg=utils.mask_credentials(system_ffmpeg),
            bundled_dir=utils.mask_credentials(os.path.realpath(bundled_dir)),
        )
    )
    return False
