---
name: dlr-dev-playbook
description: 新功能设计、测试编写、运行日志排障、跨平台故障或语法审查
---

# DLR 开发手册

约束事实源是根目录 `AGENTS.md`。本技能仅补操作配方；按当前任务读取对应参考，不通读全部文档。`ARCHIVE/` 是历史存档：默认不扫描、不全文检索、不作为事实源，仅在用户明确要求追溯历史时按精确路径读取单个文件。

| 场景 | 参考 |
| --- | --- |
| 新功能或产品行为需要设计取舍 | [设计](references/design-brainstorming.md) |
| 写测试、替换 stdlib、复现并发与只读故障 | [测试](references/testing-conventions.md) |
| 用户提供运行日志、排查异常或回退 | [日志分诊](references/log-fault-triage.md) |
| Windows 正常而其他平台失败 | [跨平台检查](references/platform-type-gating.md) |
| reviewer 质疑语法或静态检查结果 | [实测裁定](references/static-check-adjudication.md) |
| 范围不清、需要判断是否澄清 | [范围判断](references/engineering-principles.md) |
| 确需浏览器交互 mockup | [可选可视化工具](references/visual-companion.md) |

按改动选择和编写相关测试；仅在需要 commit 时运行 pytest 与覆盖率检查。日常不自动测试或全量检查，清理和文档更新遵守 AGENTS.md。
