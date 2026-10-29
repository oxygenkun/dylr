# 中文原文 / 英文目录、语言切换与 print 包装的回归测试。

import importlib.util
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from src.support import i18n

ROOT = Path(__file__).resolve().parents[1]


class TestInitialization:
    def test_no_lang_env_pollution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 原实现导入即写死 LANG=zh_CN.utf8（macOS 非法 locale），并传染子进程。
        # LT-09（2026-10-18 审查）：判据改为「执行模块体前后取值对比」，旧断言拿**宿主此刻
        # 的取值**当判据——设了 LANG=zh_CN.utf8 的开发机会恒红，宿主未设时又从未检验过真判据。
        # 基线必须先把这一族变量清空再取：本文件顶部的 `import i18n` 已跑过一次模块体，
        # 基线若在那之后取，「导入期写 LANG」会被一并吸收（变异实测：不清空时加回写入语句仍恒绿）。
        # 不用 importlib.reload：那会把线上模块的 _tr / original_print 换成第二份快照，
        # 同会话其它用例持有的引用随之漂移。
        for key in [k for k in os.environ if k.startswith(("LANG", "LC_"))]:
            monkeypatch.delenv(key, raising=False)
        before = {k: v for k, v in os.environ.items() if k.startswith(("LANG", "LC_"))}
        spec = importlib.util.spec_from_file_location("i18n_body_probe", ROOT / "src/support/i18n.py")
        assert spec is not None and spec.loader is not None
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        after = {k: v for k, v in os.environ.items() if k.startswith(("LANG", "LC_"))}
        assert after == before, f"i18n 导入期改写了语言相关环境变量: 前={before} 后={after}"
        # 反向见证：模块体确实执行到了（拿到 locale_path 才算跑过，否则上面两条空集合也相等）
        assert Path(probe.locale_path).exists()

    def test_locale_path_exists(self) -> None:
        assert Path(i18n.locale_path).exists()


class TestTranslatedPrint:
    def test_translated_print_calls_builtin_print(self) -> None:
        captured: list[tuple[object, ...]] = []
        # i18n 内部调用导入时保存的 original_print 引用，需 patch 该引用
        with patch.object(i18n, "original_print", side_effect=lambda *a, **k: captured.append(a)):
            i18n.translated_print("hello", "world", sep="-", end="")
        assert captured[0][0] == "hello-world"

    def test_should_translate_project_files(self) -> None:
        assert i18n._should_translate(str(Path(__file__).resolve())) is True

    def test_should_translate_foreign_files(self) -> None:
        assert i18n._should_translate("C:/somewhere/else/file.py") is False

    def test_should_requires_separator_boundary(self) -> None:
        # L-03：与项目根「同名前缀的兄弟目录」不是项目源码。裸 startswith(_project_root)
        # 下 /proj-evil/x.py 会被 /proj 命中，第三方目录的 print 也被翻译；补分隔符边界后必须判 False。
        sibling = f"{i18n._project_root}-evil{os.sep}evil.py"
        assert i18n._should_translate(sibling) is False
        # 反向护栏：真实子路径仍必须判 True（不得把边界补成「只认相等」而整体失明）
        child = os.path.join(i18n._project_root, "src", "streams/spider.py")
        assert i18n._should_translate(child) is True


class TestCatalogs:
    def test_supported_languages(self) -> None:
        assert set(i18n.SUPPORTED_LANGUAGES) == {"zh_CN", "en_US"}
        assert i18n.DEFAULT_LANGUAGE == i18n.FALLBACK_LANGUAGE == "zh_CN"
        assert {path.name for path in Path(i18n.locale_path).glob("*.json")} == {"en_US.json"}

    def test_english_translation(self) -> None:
        catalog = i18n._load_translations(i18n.locale_path, "en_US")
        assert catalog is not None
        assert catalog["开始录制"] == "Start Recording"
        assert all(isinstance(value, str) and value for value in catalog.values())

    def test_chinese_needs_no_catalog(self, tmp_path: Path) -> None:
        translate = i18n._build_translator(tmp_path, "zh_CN")
        assert translate("开始录制") == "开始录制"
        assert translate("plain text") == "plain text"

    @pytest.mark.parametrize("content", ["{invalid", "[]", "null", '"text"', '{"x": '])
    def test_invalid_json_returns_none(self, tmp_path: Path, content: str) -> None:
        bad = tmp_path / "en_US.json"
        bad.write_text(content, encoding="utf-8")
        assert i18n._load_json_catalog(bad) is None

    def test_missing_json_returns_none(self, tmp_path: Path) -> None:
        assert i18n._load_translations(tmp_path, "en_US") is None

    def test_json_keeps_only_string_entries(self, tmp_path: Path) -> None:
        path = tmp_path / "en_US.json"
        path.write_text('{"message": "Translation", "bad": 1}', encoding="utf-8")
        assert i18n._load_json_catalog(path) == {"message": "Translation"}


class TestLanguageSwitching:
    def setup_method(self) -> None:
        self._saved = i18n.get_language()

    def teardown_method(self) -> None:
        i18n.set_language(self._saved)

    def test_set_language_hot_swaps_translator(self) -> None:
        assert i18n.set_language("en_US") == "en_US"
        assert i18n.tr("开始录制") == "Start Recording"
        assert i18n.set_language("zh_CN") == "zh_CN"
        assert i18n.tr("开始录制") == "开始录制"

    @pytest.mark.parametrize("content", [None, "{broken", "{}", "[]"])
    def test_unavailable_english_falls_back_to_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str | None
    ) -> None:
        if content is not None:
            (tmp_path / "en_US.json").write_text(content, encoding="utf-8")
        monkeypatch.setattr(i18n, "locale_path", tmp_path)
        assert i18n.has_catalog("zh_CN")
        assert i18n.resolve_language("en_US") == "zh_CN"
        assert i18n.set_language("en_US") == "zh_CN"
        assert i18n.get_language() == "zh_CN"
        assert i18n.tr("开始录制") == "开始录制"

    @pytest.mark.parametrize("value", [None, "", "  ", "fr_FR"])
    def test_default_is_chinese(self, monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        monkeypatch.setenv("LANGUAGE", "en_US")
        assert i18n.resolve_language(value) == "zh_CN"
        assert i18n.set_language(value) == "zh_CN"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("zh-cn", "zh_CN"),
            ("zh-Hant", "zh_CN"),
            ("zh_TW", "zh_CN"),
            ("EN", "en_US"),
            ("en_GB", "en_US"),
            ("en_US.UTF-8", "en_US"),
        ],
    )
    def test_language_aliases(self, value: str, expected: str) -> None:
        assert i18n.is_recognized_language(value)
        assert i18n.normalize_language(value) == expected
        assert i18n.resolve_language(value) == expected
        assert i18n.set_language(value) == expected

    @pytest.mark.parametrize("value", [None, "", "fr", "klingon"])
    def test_unrecognized_language(self, value: str | None) -> None:
        assert not i18n.is_recognized_language(value)

    def test_available_languages_returns_copy(self) -> None:
        languages = i18n.available_languages()
        languages["xx_XX"] = "injected"
        assert "xx_XX" not in i18n.available_languages()
