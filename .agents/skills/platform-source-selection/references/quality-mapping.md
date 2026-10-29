# 画质映射检查

约束见 `AGENTS.md` 与 `docs/agent-reference/known-pitfalls.md`「画质档位与选源映射」。具体索引与 rate 以 src/streams/stream.py 的映射表为准，不在文档复制一套常量。

- 新增中文档位时核对解析、Web 关键词、main 白名单、前端选项，蓝光子档归入 BD_SUB_TIERS。
- 构造 FLV-only、HLS-only、双列表长度不同的输入，用同一原始请求分别钳制，检查 URL 与 actual_quality 对应；参考 test_tiktok_hls_quality_index.py。
- 虎牙覆盖请求档不存在、无更低档、exsphd 上限；斗鱼覆盖 rate 被钳制和两档耗尽。返回保持在播语义，不把选档失败伪装关播。
- HEVC 原画替换核对 h264 备选与下游容器，不只测主候选。
