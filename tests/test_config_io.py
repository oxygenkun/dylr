# src/config/io.py 回归锁：delete_line 的行尾归一与返回值（SEV-05），
# 以及「持锁写入 + 原子写 + URL 配置快照同步」等既有契约不被改坏。
#
# 存在理由（SEV-05）：delete_line 按 MI-11 用 newline="" 读原文（行尾带 \r\n），调用方传入的
# 却是 universal newlines 归一后的 \n 结尾行，整行精确比较在 CRLF 文件上恒不成立 → 面板点
# 「删除」静默 no-op 却回报成功。CI 跑 Linux（天然 LF）结构上发现不了，故须显式构造 CRLF 用例。
from pathlib import Path
from types import TracebackType
from typing import Self

import pytest
from pytest import MonkeyPatch

from src.config import io as config_io
from src.recording import state


class _RecordingLock:
    # 替身锁：记录 with 进入次数，用于断言「持锁读写」这一既有契约未被改坏
    # （AGENTS：函数要么只在内部加锁、要么只由调用方加锁，二选一须可机检）。
    def __init__(self) -> None:
        self.entered = 0

    def __enter__(self) -> Self:
        self.entered += 1
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


def _write_lines(path: Path, lines: list[str], encoding: str = "utf-8-sig") -> None:
    # newline="" 写入：不做换行翻译，逐字保留 \r\n（Windows 上 URL_config.ini 的真实形态）。
    # 用默认换行写只会得到 LF，而 CI 恰是天然 LF——只有显式构造 CRLF 才能暴露「读侧行尾不匹配」。
    with open(path, "w", encoding=encoding, newline="") as f:
        _ = f.write("".join(lines))


def _read_raw(path: Path, encoding: str = "utf-8-sig") -> list[str]:
    # 同样按 newline="" 读回：断言「文件字节形态」而非 universal newlines 修饰后的视图，
    # 否则「行尾是否被改成 LF」这一子问题永远断不出来。
    with open(path, encoding=encoding, newline="") as f:
        return f.readlines()


class TestDeleteLineLineEndings:
    # SEV-05：CRLF 文件 + \n 结尾的入参（调用方的真实形态）必须真的删掉行。

    def test_crlf_file_with_lf_argument_removes_line(self, tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
        target = tmp_path / "URL_config.ini"
        kept_url = "https://live.douyin.com/111111111111\n"
        gone_url = "https://live.douyin.com/222222222222\n"
        _write_lines(target, [kept_url.replace("\n", "\r\n"), gone_url.replace("\n", "\r\n")])
        monkeypatch.setattr(state, "url_config_file", str(tmp_path / "other.ini"))

        # 调用方传的是 universal newlines 读出的 \n 结尾行（web_api 的 raw_line / main 的 origin_line）
        assert config_io.delete_line(str(target), gone_url) is True

        remaining = _read_raw(target)
        assert [kept_url.replace("\n", "\r\n")] == remaining
        # 未被整体改写成 LF：保留原文件的 CRLF 行尾（MI-11 语义不变）
        assert all(line.endswith("\r\n") for line in remaining)

    def test_lf_file_still_removes_line(self, tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
        target = tmp_path / "URL_config.ini"
        _write_lines(target, ["https://a.example/1\n", "https://a.example/2\n"])
        monkeypatch.setattr(state, "url_config_file", str(tmp_path / "other.ini"))

        assert config_io.delete_line(str(target), "https://a.example/1\n") is True
        assert _read_raw(target) == ["https://a.example/2\n"]

    def test_missing_line_returns_false_and_keeps_file(self, tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
        target = tmp_path / "URL_config.ini"
        original = ["https://a.example/1\r\n", "#https://a.example/2\r\n"]
        _write_lines(target, original)
        monkeypatch.setattr(state, "url_config_file", str(tmp_path / "other.ini"))

        assert config_io.delete_line(str(target), "https://a.example/999\n") is False
        assert _read_raw(target) == original

    def test_comment_prefix_not_matched_by_bare_url(self, tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
        # 精确整行比较的语义必须保留：被注释掉（# 前缀）的行不等于该 URL 本身
        target = tmp_path / "URL_config.ini"
        original = ["#https://a.example/1\r\n"]
        _write_lines(target, original)
        monkeypatch.setattr(state, "url_config_file", str(tmp_path / "other.ini"))

        assert config_io.delete_line(str(target), "https://a.example/1\n") is False
        assert _read_raw(target) == original

    def test_delete_all_removes_every_match(self, tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
        target = tmp_path / "URL_config.ini"
        dup = "https://a.example/1\r\n"
        _write_lines(target, [dup, "https://a.example/2\r\n", dup])
        monkeypatch.setattr(state, "url_config_file", str(tmp_path / "other.ini"))

        assert config_io.delete_line(str(target), "https://a.example/1\n", delete_all=True) is True
        assert _read_raw(target) == ["https://a.example/2\r\n"]

    def test_single_match_only_by_default(self, tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
        target = tmp_path / "URL_config.ini"
        dup = "https://a.example/1\r\n"
        _write_lines(target, [dup, dup])
        monkeypatch.setattr(state, "url_config_file", str(tmp_path / "other.ini"))

        assert config_io.delete_line(str(target), "https://a.example/1\n") is True
        assert _read_raw(target) == [dup]

    def test_read_failure_returns_false(self, tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
        missing = tmp_path / "not-here.ini"
        monkeypatch.setattr(state, "url_config_file", str(tmp_path / "other.ini"))
        assert config_io.delete_line(str(missing), "https://a.example/1\n") is False


class TestDeleteLineWriteContract:
    # 既有契约：持锁 + 原子写 + 命中 URL 配置文件时同步快照。

    def test_holds_file_update_lock(self, tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
        target = tmp_path / "URL_config.ini"
        _write_lines(target, ["https://a.example/1\r\n", "https://a.example/2\r\n"])
        recorder = _RecordingLock()
        monkeypatch.setattr(state, "file_update_lock", recorder)
        monkeypatch.setattr(state, "url_config_file", str(tmp_path / "other.ini"))

        assert config_io.delete_line(str(target), "https://a.example/1\n") is True
        assert recorder.entered == 1

    def test_atomic_write_failure_returns_false(self, tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
        # 写盘失败必须回报 False（调用方据此报错，不能再回报成功）
        target = tmp_path / "URL_config.ini"
        original = ["https://a.example/1\r\n"]
        _write_lines(target, original)
        monkeypatch.setattr(state, "url_config_file", str(tmp_path / "other.ini"))
        monkeypatch.setattr(config_io, "_atomic_write_text", lambda *a, **kw: False)

        assert config_io.delete_line(str(target), "https://a.example/1\n") is False
        assert _read_raw(target) == original

    def test_snapshot_updated_for_url_config_file(self, tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
        target = tmp_path / "URL_config.ini"
        _write_lines(target, ["https://a.example/1\r\n", "https://a.example/2\r\n"])
        monkeypatch.setattr(state, "url_config_file", str(target))
        monkeypatch.setattr(state, "ini_URL_content", "OLD-SNAPSHOT")

        assert config_io.delete_line(str(target), "https://a.example/1\n") is True
        # 快照更新为「已落盘内容」，供 update_file 的异常恢复基线使用
        assert state.ini_URL_content == "https://a.example/2\r\n"


class TestMissingKeyWritebackIdempotency:
    # 「缺键补写必须幂等」：同一份 TOML 连续两轮「读取 + 缺键补写」，该键只能出现一次；
    # TOML 本身禁止重复键，重复写入会让整份配置解析失败。
    _KEY = "是否启动时清理陈旧字节码缓存(是/否)"

    def _assert_readable(self, target: Path) -> None:
        from src.config.store import ConfigStore

        store = ConfigStore(target)
        store.read()
        assert store.native("录制设置", self._KEY) in (True, False)

    def test_two_rounds_of_bool_writeback_leave_exactly_one_line(
        self, tmp_path: Path, monkeypatch: MonkeyPatch
    ) -> None:
        from src.config.store import ConfigStore

        target = tmp_path / "config.toml"
        target.write_text('["录制设置"]\n"循环时间(秒)" = 60\n', encoding="utf-8")
        monkeypatch.setattr(state, "config_file", str(target))
        store = ConfigStore(target)
        store.read()

        for _ in range(2):
            assert config_io.read_config_bool(store, "录制设置", self._KEY, True) is True

        written = target.read_text(encoding="utf-8")
        assert written.count(self._KEY) == 1, written
        self._assert_readable(target)
        again = ConfigStore(target)
        again.read()
        assert again.native("录制设置", self._KEY) is True

    def test_existing_key_is_never_appended_again(self, tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
        # 键已存在时只读取、不得再补写；用户显式写的 false 也不能被默认值覆盖。
        from src.config.store import ConfigStore

        target = tmp_path / "config.toml"
        target.write_text(f'["录制设置"]\n"{self._KEY}" = false\n', encoding="utf-8")
        monkeypatch.setattr(state, "config_file", str(target))
        store = ConfigStore(target)
        store.read()

        for _ in range(2):
            assert config_io.read_config_bool(store, "录制设置", self._KEY, True) is False

        assert target.read_text(encoding="utf-8").count(self._KEY) == 1
        self._assert_readable(target)


if __name__ == "__main__":  # pragma: no cover - 仅手工单文件调试入口
    raise SystemExit(pytest.main([__file__, "-q"]))
