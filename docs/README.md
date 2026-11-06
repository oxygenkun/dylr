# 文档导航

使用入口是 [README](../README.md)；实现说明从[架构导航](architecture/README.md)进入；工程规则以 [AGENTS.md](../AGENTS.md) 为准。

## 使用

| 文档 | 内容 |
| --- | --- |
| [README](../README.md) | 简介、功能、安装、平台清单与快速开始 |
| [配置说明](configuration.md) | TOML 键位、推送 / Cookie / Web 配置、直播间 URL 写法与环境变量 |
| [使用说明](usage.md) | 启动、录制格式、画质、HLS/FLV、弹幕、多语言与停止方式 |
| [部署](deployment.md) | 运行包命名、Docker、GHCR、挂载与端口 |
| [常见问题](faq.md) | FFmpeg、Node、风控、代理、HLS 与录制故障 |

## 开发与实现

| 文档 | 内容 |
| --- | --- |
| [开发指南](development.md) | 环境、提交检查、测试组织、新平台接入 |
| [架构导航](architecture/README.md) | 系统流程、依赖边界与子系统专题 |
| [项目结构](agent-reference/project-structure.md) | 源码树、工具与运行期目录 |
| [构建与发布](build-and-release.md) | 打包、冒烟、CI、发行与镜像 |

## 参考

| 文档 | 内容 |
| --- | --- |
| [已知坑](agent-reference/known-pitfalls.md) | 按主题的约束、回归锁与实测依据入口 |
| [实测依据](agent-reference/measured-evidence.md) | 带日期的读数，不充当当前配置模板 |

## 变更

| 文档 | 内容 |
| --- | --- |
| [实现变更记录](changelog/implementation-notes.md) | 当前周期的详细实现变更 |
| [发布说明](changelog/release-notes-zh.md) | 面向用户的版本摘要 |

## 历史

[ARCHIVE](../ARCHIVE/README.md) 保存与当前实现不兼容的旧文档、审查、提案与旧发布说明；默认不扫描、不作为事实源，只在明确追溯时按精确路径读取单个文件。

- [早期实现记录](../ARCHIVE/changelog/code-wiki-history-zh.md)
- [早期发布说明](../ARCHIVE/changelog/release-notes-history-zh.md)
- [历史文件清单](../ARCHIVE/agent-reference/changelog-file-inventories.md)
- [共享经验](../ARCHIVE/agent-reference/session-learnings.md)、[锁分类快照](../ARCHIVE/agent-reference/lock-classification.md)
- [2026-10-17 模块改动快照](../ARCHIVE/worklog/change-overview-2026-10-17.md)

## 维护规则

- README 只保留使用入口与概览；配置、使用、部署细节留在上表对应文档。
- 配置示例归[配置说明](configuration.md)与 [TOML 模板](../config/config.toml.example)；目录树归[项目结构](agent-reference/project-structure.md)；实现说明归架构专题；长期规则归 [AGENTS.md](../AGENTS.md)。
- 历史版本、审查与提案只在 [ARCHIVE](../ARCHIVE/README.md) 维护，不复制到当前文档。
- 新增或移动文档时同步本页的分组与链接，保持入口唯一。
