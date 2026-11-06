# Docker 只承载 CLI / Web，并通过锁文件安装基础依赖。

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"


def test_image_installs_locked_base_dependencies_only() -> None:
    # 只看可执行指令，历史说明里的包名不应影响安装契约。
    text = DOCKERFILE.read_text(encoding="utf-8")
    instructions = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "uv sync --locked --no-dev" in instructions
    assert "COPY pyproject.toml uv.lock ./" in instructions
    assert "--extra" not in instructions
    assert "INSTALL_GUI" not in instructions
    assert "PySide6" not in instructions
    assert "libxcb" not in instructions
    assert "libegl1" not in instructions


def test_compose_defines_single_entry_service() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yaml").read_text(encoding="utf-8"))
    assert set(compose["services"]) == {"recorder"}
    service = compose["services"]["recorder"]
    assert "INSTALL_GUI" not in service["build"].get("args", {})
    # 面板开关只在 config.toml，compose 不得再注入覆盖入口行为的变量
    assert "DOUYIN_WEB_ENABLE" not in service.get("environment", {})


def test_docker_context_contains_lock() -> None:
    patterns = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "uv.lock" not in patterns


def test_published_image_workflow_has_no_gui_option() -> None:
    text = (ROOT / ".github/workflows/docker-publish.yml").read_text(encoding="utf-8")
    assert "INSTALL_GUI" not in text
    assert "--extra gui" not in text


def test_project_runs_from_source_without_package_installation() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert metadata["tool"]["uv"]["package"] is False
    assert "build-system" not in metadata
    assert "scripts" not in metadata["project"]
