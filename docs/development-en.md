# Development Guide

[Back to README](../README_EN.md) · [简体中文](development.md)

> Development checks: run pytest and coverage checks only when a commit is needed. Routine edits do not start tests automatically. See [AGENTS.md](../AGENTS.md) for current policy; retired tools in historical release notes are not current requirements.

## Environment requirements

- Python >= 3.14
- FFmpeg and Node.js: use existing executables or download directly from the resource manifest into the app directory. Install manually if downloading fails.
- Automatic macOS FFmpeg downloads require gpg; Docker already includes its runtimes.

## Install dev dependencies

```bash
# Install runtime dependencies and development tools
uv sync --locked --extra dev
```

Declare dependencies only in `pyproject.toml` and lock them in `uv.lock`. Use `uv add` / `uv remove` for runtime dependencies; add `--optional dev` or `--optional build` for development or build tools, and commit the corresponding lock-file changes.

## Commit hooks

The dev extra installs prek, which reuses `.pre-commit-config.yaml`. YAML, TOML and file-size checks use built-in hooks; Ruff uses the project-locked version.

```bash
uv run --locked --extra dev prek install
uv run --locked --extra dev prek run --all-files
```

Install the hook once per clone. If a pre-commit hook is already installed, review it for custom logic before replacing it with `prek install --force`.

## Code conventions

```bash
# Format code (line-length = 120)
uv run --locked --extra dev ruff format .

# Static checks (add --fix to apply safe fixes)
uv run --locked --extra dev ruff check .

# Collect and check coverage when preparing a commit
uv run --locked --extra dev python -m pytest --cov=src
uv run --locked --extra dev python tool/check_coverage.py
```

> **Comment convention**: see the code style rules in [AGENTS.md](../AGENTS.md).

> **Quality gates**: Ruff formats code and checks common errors, import ordering, modern Python syntax, redundant code, leftover debuggers, and `noqa` validity; `pyproject.toml` defines the exact rules and exceptions. pytest requires zero warnings, and there is no permanent type-checker gate. See [AGENTS.md](../AGENTS.md) for check timing, safe fixes, and exceptions; routine edits do not automatically run pytest or coverage.

> **Test note**: `tests/test_web_api.py`'s symlink-related cases (`TestListFiles::test_broken_symlink_skipped` / `test_symlink_outside_skipped`) auto-`pytest.skip` in environments where real symlinks cannot be created (Windows without Developer Mode, some sandboxes) — this is normal and does not indicate a code defect.

## Project documentation

- [Directory and responsibility map](../docs/agent-reference/project-structure.md) - full source tree, tools, tests, docs and runtime directories
- [CODE_WIKI.md](../CODE_WIKI.md) - module descriptions, dependencies and design patterns (Chinese)
- [AGENTS.md](../AGENTS.md) - engineering conventions and check commands

## Web/API smoke testing

The project ships a generic, zero-dependency Web/API smoke-test tool `tool/smoke_test.py` (pure standard library, no third-party packages needed), which can do lightweight liveness probes against **running HTTP interfaces** such as the Web management panel.

```bash
# Check the local Web management panel (default 127.0.0.1:8000; sample config in tool/smoke_web.json)
python tool/smoke_test.py -c tool/smoke_web.json

# Generate an HTML report
python tool/smoke_test.py -c tool/smoke_web.json -r smoke_report.html -f html
```

- Config-driven (JSON): `url` / `method` / `expected_status` / `timeout` / request headers / request body / response text that should be included / expected JSON fields
- Supports `base_url` prefix concatenation; console / JSON / HTML report formats; non-zero exit code on failure (can be wired into CI)
- Unlike `tool/build_exe.py --smoke` (packaging-artifact smoke test), this tool probes **running HTTP interfaces** for liveness — the two complement each other
- The default case `tool/smoke_web.json` probes the Web panel `/` (home page 200) and `/health` (`{"status": "ok"}`, a public liveness endpoint provided by `src/api/routes.py`, not gated by `web_auth_enable`)
- **Wired into CI**: the `test` job in `.github/workflows/ci.yml` sets `web_enable = true` in `config/config.toml` and starts the panel in a controlled way after pytest with `python -m src.main`, then runs the `tool/smoke_web.json` smoke test once it is ready (network flakiness handled via `.github/actions/retry`; on failure it keeps the `logs/web-smoke-*` artifacts and turns the job red), covering the "can the panel process really bind and respond" path that TestClient cannot

## Add a new platform

**Video recording platform:**

1. Add a platform data-fetching function in `src/streams/spider.py` (refer to existing platform implementations)
2. Add a stream-URL parsing function in `src/streams/stream.py`, with the return value containing `actual_quality` and `available_qualities` fields
3. Register URL admission, the platform resolver and room context in `src/streams/dispatch.py`
4. Update `README.md` and `CODE_WIKI.md`

**Danmaku recording platform (if danmaku support is needed):**

1. Create `<platform>.py` under `src/danmaku/platforms/`, inheriting `DanmakuBase` from `src/danmaku/base.py`, implementing connect/auth/message parsing
2. Register it in the danmaku platform registry in `src/__init__.py` (platform name must match the `platform` id in `src/streams/dispatch.py`); decoupled creation via the `get_danmaku_class` / `get_danmaku_collector` factory
3. Populate danmaku arguments in the resolver in `src/streams/dispatch.py`; `src/recording/session.py` uses the shared collector lifecycle
4. Update `README.md` and `CODE_WIKI.md`

> Note: the danmaku subsystem and `src/streams/spider.py` (video stream-URL parsing) are two parallel, decoupled abstractions; `spider.py` does not import `src/danmaku/platforms/`.
