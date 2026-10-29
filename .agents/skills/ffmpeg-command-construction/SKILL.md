---
name: ffmpeg-command-construction
description: 修改 ffmpeg 录制命令、保存类型、输入输出参数或分段容器映射
---

# ffmpeg 命令构造

约束事实源：`AGENTS.md`；ffmpeg 命令与容器的长期约束见 `docs/agent-reference/known-pitfalls.md`「ffmpeg 命令构造与容器格式」。先找构造入口和调用方，不在平台分支复制命令。

## 修改落点

| 改动 | 入口 / 联动 |
| --- | --- |
| 输入选项、headers、代理、TLS | recording._build_ffmpeg_input_args，按 -i 锚点操作，不用裸列表下标 |
| 输出扩展名与分段路径 | _build_record_output_path、_build_ffmpeg_output_args 与现有格式表 |
| 执行与录后转换 | _run_ffmpeg_record、_convert_after_record |
| 平台解析分发 | platform_dispatch._PLATFORM_RESOLVERS 与 _PlatformResolveContext，表顺序决定优先级 |
| 失败/成功反馈 | check_subprocess 与 stream_select 退避接线 |

新增 per-file 选项先用目标版本 `ffmpeg -h full` 确认输入/输出归属，再检查参数位置和支持版本；兼顾镜像现有版本，不只看最新版本。

保存类型变化时联查扩展名、SEGMENT_FORMAT_BY_SUFFIX、HEVC 能力与分段命名；容器名从映射表取，不能按文件后缀猜。安全修复同时核对 standalone 副本。

## 验证选择

- 参数位置、HLS 重连、队列兼容：tests/test_ffmpeg_reconnect_args.py。
- 容器与分段：tests/test_record_container.py。
- 完整命令：tests/test_start_record_command_golden.py；只有预期行为变化时重生成 golden，并审查差异。
- 平台顺序：tests/test_platform_dispatch.py。
- 调度与退避反馈：tests/test_record_failure_feedback.py。

按改动选择用例；仅在需要 commit 时运行 pytest 与覆盖率检查。真机验证与清理遵守 AGENTS.md。
