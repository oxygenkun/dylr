"""统一入口：先按 config.toml 的 [Web] 配置决定是否同时启动管理面板，再运行录制引擎。"""


def main() -> None:
    """启动录制；`[Web] web_enable` 为真时同时启动管理面板。"""
    from src.support.console import configure_encoding

    configure_encoding()
    from src.config.store import ensure_config_migrated
    from src.recording import state

    _ = ensure_config_migrated(state.config_file, state.legacy_config_file)
    from src.api.config import read_web_config

    if read_web_config(state.config_file)["web_enable"]:
        from src import web

        web.run()
        return

    from src.recording import engine as recorder

    recorder.initialize_process(cli=True)
    recorder.main()


if __name__ == "__main__":
    main()
