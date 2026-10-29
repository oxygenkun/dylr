"""CLI 入口：设置控制台后启动录制引擎。"""


def main() -> None:
    from src.support.console import configure_encoding

    configure_encoding()
    from src.recording import engine as recorder

    recorder.initialize_process(cli=True)
    recorder.main()


if __name__ == "__main__":
    main()
