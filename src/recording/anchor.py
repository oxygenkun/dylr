"""主播改名后的目录与文件名同步。"""

import os

from loguru import logger

from src.config import paths as config_paths
from src.config import settings
from src.support import i18n


def rename_anchor_directory(old_name: str, new_name: str, platform: str) -> bool:
    """主播名变更后同步文件系统：把 {保存路径}/{platform}/{old_name} 改名为 new_name。"""
    if not old_name or not new_name or old_name == new_name:
        return True
    try:
        save_root = settings.video_save_path or config_paths.default_path
        platform_dir = os.path.join(save_root, platform)
        if not os.path.isdir(platform_dir):
            return True
        old_dir = os.path.join(platform_dir, old_name)
        new_dir = os.path.join(platform_dir, new_name)
        if os.path.isdir(old_dir):
            if os.path.isdir(new_dir):
                _merge_anchor_directory(old_dir, new_dir)
            else:
                os.rename(old_dir, new_dir)
        _rename_prefixed_entries(platform_dir, old_name, new_name)
        return True
    except OSError as e:
        logger.warning(
            i18n.tr(
                "重命名主播目录失败（下轮重试）: {old_name} -> {new_name}: {e}",
                old_name=old_name,
                new_name=new_name,
                e=e,
            )
        )
        return False


def _merge_anchor_directory(old_dir: str, new_dir: str) -> None:
    for entry in list(os.scandir(old_dir)):
        dst = os.path.join(new_dir, entry.name)
        if os.path.exists(dst):
            logger.warning(i18n.tr("合并主播目录时发现同名文件（双方保留，请手动整理）: {dst}", dst=dst))
            continue
        try:
            os.rename(entry.path, dst)
        except OSError as e:
            logger.warning(
                i18n.tr(
                    "合并主播目录条目失败（下轮重试）: {path} -> {dst}: {type_name}: {e}",
                    path=entry.path,
                    dst=dst,
                    type_name=type(e).__name__,
                    e=e,
                )
            )
    try:
        if not os.listdir(old_dir):
            os.rmdir(old_dir)
    except OSError as e:
        logger.warning(
            i18n.tr(
                "删除旧主播目录失败（已忽略）: {old_dir}: {type_name}: {e}",
                old_dir=old_dir,
                type_name=type(e).__name__,
                e=e,
            )
        )


def _rename_prefixed_entries(base_dir: str, old_name: str, new_name: str) -> None:
    try:
        entries = list(os.scandir(base_dir))
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                _rename_prefixed_entries(entry.path, old_name, new_name)
                dir_name = entry.name
                if dir_name != new_name and dir_name.endswith(f"_{old_name}"):
                    os.rename(entry.path, os.path.join(base_dir, dir_name[: -len(old_name)] + new_name))
            elif entry.name.startswith(f"{old_name}_"):
                os.rename(entry.path, os.path.join(base_dir, f"{new_name}_{entry.name[len(old_name) + 1 :]}"))
        except OSError as e:
            logger.warning(
                i18n.tr(
                    "主播名变更重命名失败（已跳过，不影响其余文件）: {path}: {type_name}: {e}",
                    path=entry.path,
                    type_name=type(e).__name__,
                    e=e,
                )
            )
