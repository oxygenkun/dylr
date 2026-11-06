# 工作区改动快照

[文档索引](../../docs/README.md) · [实现变更记录](../../docs/changelog/implementation-notes.md)

> 本卷保存 CODE_WIKI 中 2026-10-17 的按模块改动总览，是当时工作区与验证读数的快照，不代表现行配置、路径或检查要求。原范围、历史描述与代码片段保留。

## 改动总览（按模块分类，2026-10-17）

> 范围口径：基线 `72d1664`（本会话开始时的 HEAD）→ 当前工作区（读数取 2026-10-17 23:00），覆盖**已提交**（`553b036`）、**未提交**与**仅存在于暂存区**三类变更。本轮**没有删除文件**；被移除的只有 i18n 目录里的 6 条孤儿条目（见第 1 节），配置键未被移除。
>
> 并发工作包提醒：`src/config/reader.py` 与 `tests/test_config_read.py`（未跟踪）及 `main.py`、`src/gui_qt/{settings_dialog,widgets,danmaku}.py`、`src/ui_{motion,scale,theme}.py`、`tests/test_config_io.py` 的改动**不属本代理**，不计入下表；它新增的模板 `config.ini 存在重复配置项，已按最后一次取值读取: {detail}` 曾有一度未登记，使 `tests/test_i18n_migration.py::test_runtime_templates_covered_by_catalog` 短暂为红（当时提取器报的唯一缺失项即此条）；该包随后自行补齐四目录，复测缺失 0 条、`test_i18n_migration.py` + `test_i18n_raise_site_tr.py` 8 passed。
>
> 标注图例：〔破坏性〕改变对外契约 / 镜像内容 / 安装产物；〔配置〕涉及配置项或默认值；〔依赖〕涉及依赖或版本常量；〔字符串契约〕改动的是可能被别处按字面比对的文本。

### 1. 本地化目录（i18n）

- **修改内容** · `i18n/zh_CN/LC_MESSAGES/zh_CN.po`、`i18n/en_US.json`、`i18n/en_GB.json`、`i18n/zh_TW.yaml` — 补 5 条 Qt 侧栏导航 msgid（两空格形态，按名传入 `tr()`，提取器看不见），四目录键数 1110 → 1115
- **修改内容** · 同上四份目录 — 〔字符串契约〕补 16 条 raise 位中文文案 msgid（`main.py` 7 条 / `src/streams/spider.py` 3 条 / `src/api/models.py` 6 条），键数 1115 → 1131
- **修改内容** · 同上四份目录 — 补 1 条字体复制失败的日志模板 msgid，占位符为 `{name}`/`{target}`/`{type_name}`/`{err}`，键数 1131 → 1132
- **修改内容** · `i18n/zh_CN/LC_MESSAGES/zh_CN.mo` — 由 `scripts/compile_po.py` 重编：1133 条 / 149 505 字节（含头部空 msgid，N == 键数 + 1）；四目录键集合逐项相等；`.po` 头部日期更新为 2026-10-17
- **修改内容** · 同上四份目录 — **收回 6 条孤儿裸文本键**（`停止录制`/`重新读取`/`保存 URL 配置`/`打开下载目录`/`高级设置`/`清空日志`）：它们是 GUI 层改带图标键后失去调用点的历史条目，AST 全仓扫字符串常量确认无任何消费者（`停止录制` 只在 `web/app.js` 内嵌目录里作为**值**出现，与后端键无关）。四目录键数 1132 → 1126，`.mo` 重编为 1127 条 / 149 231 字节
- **说明** · `开始录制`（四目录保留） — 同形裸键但**不收回**：`tests/test_i18n.py::test_load_translations_priority` 与 `::test_set_language_hot_swaps_translator` 拿它当样本 msgid 验证「`.mo` 优先于 JSON/YAML」的三档加载与热切换，收回即打红两条机制用例。该耦合已写进 `AGENTS.md` 盲区清单

### 2. GUI 窗口层（`src/gui_qt/`）

- **修改内容** · `src/gui_qt/main_window.py`、`src/gui_qt/pages/dashboard.py`、`src/gui_qt/pages/config.py`、`src/gui_qt/pages/runlog.py` — 9 处按钮的构造标签与 `T()` 登记键统一为**同一个带 emoji 的既有目录键**（三空格形态），修掉「启动回填与每次 `retranslate_all()` 把图标打回裸文本」的故障；四目录零新增
- **新增功能** · 同上 4 个文件 — 在配对处补「构造标签必须与登记键同 msgid」的理由注释：完整表述留 `main_window.py`，其余三个文件各一行指向它（避免第二事实源）

### 3. GUI 字体模块（`src/ui_fonts.py`）

- **修改内容** · `src/ui_fonts.py::install_font_file` — 〔依赖〕`except Exception: return None` 之前先落一行 `logger.warning(i18n.tr("复制字体文件失败: {name} -> {target} - {type_name}: {err}", name=src.name, target=str(target), type_name=type(e).__name__, err=e))`；被占用 / 目录只读 / 磁盘满 / 源文件被移走这几种在 UI 上同形的成因从此可归因。模块新增 `import i18n` 与 `from src.support.logger import logger`
- **修改内容** · `src/ui_fonts.py::_safe_filename`、`src/ui_fonts.py::_WIN_RESERVED_NAMES` — 〔配置〕新增 Windows 保留设备名清洗（`CON`/`PRN`/`AUX`/`NUL`/`COM1-9`/`LPT1-9`：整段基名、大小写不敏感、加 `_` 前缀且幂等；`COM10` 刻意不算）。理由是跨构建/跨 API 的可移植性——本机 Windows 10.0.26300 实测 `CON.ttf`/`AUX.ttf` 反而能落成普通文件；清洗后的名字会写进 `gui_font_file` 持久化并在下次启动读回

### 4. 录制链与平台解析（`main.py` / `src/streams/spider.py`）

- **修改内容** · `main.py`（`_hy_fan_path_segment`、`_b23_tv_path_segment` 及两支短链解析器，共 12 处） — 〔字符串契约〕中文 raise 文案改为在产生点 `i18n.tr(...)`：这些消息原先被当 `{reason}` 实参插进一条已翻译的日志模板，模板查表、消息从不查表，所以只有产生点接表才有目录意义。成功路径未改动
- **修改内容** · `src/streams/spider.py`（TTingLive 登录、TwitCasting、微博，共 3 处） — 〔字符串契约〕TTingLive 登录失败整句接 `tr()`；TwitCasting/微博两处 f-string 改写为常量模板 + 命名实参 `{masked_url}`（脱敏仍由调用方求值后传入），与既有斗鱼站点同形态
- **说明** · `src/streams/spider.py::XhsUrlError` 的 9 条消息 — 刻意**不接** `tr()`、**不登记**：`src/streams/spider.py` 两处 `except XhsUrlError` 与 `main._resolve_xhslink_com` 一律只读 `e.code`，message 从不显示，登记即造死条目。已由 `tests/test_i18n_raise_site_tr.py` 的白名单集合相等钉住

### 5. Web 面板后端（`src/api/models.py`）

- **修改内容** · `src/api/models.py`（`_as_str`/`_as_opt_str`/`_as_bool`/`_as_str_list`/`_require_dict`，共 6 处） — 〔字符串契约〕422 校验文案接 `tr()`，f-string 改常量模板 + 命名实参 `field=`（不变量①：模板必须是字面量、占位符只能是纯标识符）
- **修改内容** · `src/api/models.py` 导入段 — 〔依赖〕新增 `import i18n`（该模块「零依赖」原指不引 pydantic/fastapi；`i18n` 只依赖 stdlib + loguru，不回指任何 `web_*` 模块，无环）。面板按 `detail` **键**取错误文本、未钉具体文案，故前端契约不受影响

### 6. 容器与部署（`Dockerfile` / `docker-compose.yaml`）

- **修改内容** · `Dockerfile`（两阶段各一条 `ARG INSTALL_GUI=false`） — 〔破坏性〕〔配置〕镜像的 GUI 依赖改由单开关控制：阶段 1 默认从依赖清单剔除 PySide6，阶段 2 仅在开关为 true 时装 Qt 运行库（`libEGL`/`libGL`/glib/fontconfig/xkbcommon 等）。默认 false 使只跑 `main.py`/`web.py` 的镜像不再装整只轮子
- **修改内容** · `docker-compose.yaml`（`gui` 服务） — 〔配置〕`gui` 服务显式传 `INSTALL_GUI: "true"` 并改用独立 tag `:latest-gui`——build 配置不同的服务共用同一 image 名时，后构建的一方会覆盖另一方正在使用的 tag
- **说明** · `.github/workflows/docker-publish.yml` — 发布链**不传** true，对外镜像保持 slim（该文件本轮未改，作为约束的另一半登记在此）

### 7. CI 与检查工具链（`ci.yml` / `.pre-commit-config.yaml` / `pyrightconfig.json`）

- **修改内容** · `.github/workflows/ci.yml` — 〔依赖〕`black_version` 26.5.1 → 26.10.1，与本机实装同值；抬值前用两版各跑一次全量 `black --check`，两版结论逐字相同（这才是判据本体）——抬常量只消除版本差，不等于 CI 实跑已验证
- **修改内容** · `.pre-commit-config.yaml` — 〔依赖〕black mirror rev 25.9.0 → 26.10.1、isort 6.1.0 → 9.0.2，使「本地 venv / CI 钉定 / 提交钩子」三面只剩一个版本值；两个 rev 的存在性经 GitHub API 核实、`validate-config` 通过，但本机 clone 被代理拦住，「钩子真的装起来跑一遍」仍未验证
- **新增功能** · `pyrightconfig.json` — 〔配置〕IDE 语言服务器与 `pyright` CLI 的共用配置：include 逐项等于 `[tool.mypy].files`、exclude 覆盖 mypy 与 basedpyright 两份清单、typeCheckingMode/pythonVersion/venv 同 `[tool.basedpyright]`。此后不带路径跑 `pyright` 即复现 IDE 扫描面；Pylance 本体因许可证不得被 shell 驱动，这条才是「IDE 结论可复核」的正解

### 8. 包元数据与派生产物（`pyproject.toml` / `egg-info` / `coverage.json`）

- **修改内容** · `pyproject.toml`（`[tool.setuptools]` 的 `packages`） — 〔破坏性〕〔依赖〕增补 `src.gui_qt`、`src.gui_qt.pages`：显式 `packages` 不递归发现，漏列会让 `pip install .` 产出的发行包缺这两个子包，`douyin-recorder-gui` 入口在导入链第一段就 `ModuleNotFoundError`
- **修改内容** · `DouyinLiveRecorder.egg-info/SOURCES.txt` — 〔依赖〕派生产物随上条重建（`python scripts/sync_metadata.py`）。`uv.lock` 与 egg-info 都是 pyproject 的派生消费方，不得手改版本字段；`sync_metadata.py --check` 复验一致
- **修改内容** · `coverage.json` — 由 `pytest --cov=src --cov-report=json` 重新生成：总覆盖 83.02% → 83.04%；`scripts/check_coverage.py` 报 68 个模块全部达标（`src/proto/douyin_pb2.py` 为显式豁免的 protoc 生成物）

### 9. 配置基线（`config/config.ini`）

- **修改内容** · `config/config.ini`（已提交部分） — 〔配置〕补齐默认键并与 `README.md`/`README_EN.md` 配置块、代码 fallback 逐项对账。该文件是**已跟踪的脱敏基线模板**，`.gitignore` 对它不生效，本地填入真实 Cookie / 代理 / 面板密码后不得提交
- **待处置** · `config/config.ini`（仅存在于暂存区的一行） — 〔破坏性〕〔配置〕暂存副本含同一节内的重复键 `是否启动时清理陈旧字节码缓存(是/否)`（第 5 行与第 44 行同处 `[录制设置]`），会让 `RawConfigParser()`（默认 `strict=True`）在下一次 `read()` 抛 `DuplicateOptionError`，即基线配置不可读。工作区已还原为与 HEAD 一致；**索引里那份未动**，清理动作 `git restore --staged config/config.ini` 交回维护者。写入侧根因未定位（单跑一次读取+补写不改写文件、`config_parser.write()` 只产出 1 份键、复跑整条 `run_gates.py` 也未复现），故本轮不声称已修，危害与复现判据已写进 `AGENTS.md`

### 10. 测试（`tests/`）

- **新增功能** · `tests/test_i18n_catalog_coverage.py`（3 用例） — 把 i18n 盲区④从「人工登记」升级为机检：按名传入 `tr()`/`T()` 的候选 msgid 必须四目录齐全、四目录键集合逐项相等、解析不出的调用点走白名单集合相等（新增即红，消解后不收回也红）
- **新增功能** · `tests/test_i18n_raise_site_tr.py`（2 用例） — 盲区⑤：未接 `tr()` 的中文 raise 站点走白名单集合相等（现 10 条），已接的 msgid 断言四目录齐全，并用锚点防「解析结果为空 = 假绿」
- **新增功能** · `tests/test_gui_qt_label_parity.py`（2 用例） — AST 收集「构造标签」与「`T()` 登记键」两张表按控件属性名配对判等，另设带 emoji 配对下限（9）与「登记键必须四目录齐全」两条判据；行为侧证据走 offscreen 真建控件读 `text()`
- **新增功能** · `tests/test_dockerfile_image_gui_arg.py`（5 用例） — 镜像 GUI 开关的结构锁：`ARG` 声明早于使用（按 Docker 阶段切分，`ARG` 不跨阶段）、过滤表达式确实选中 PySide6 且零误伤、compose 两服务 tag 互异、发布工作流不得传 true，另含判据自检
- **新增功能** · `tests/test_pyright_config_parity.py`（5 用例） — 钉住 `pyrightconfig.json` 与 `[tool.mypy].files`、basedpyright 两份清单的同源性（include 逐项相等、exclude 覆盖两份清单、严格度/版本/venv 同值、三个根入口反向见证），并禁止把 Pylance 当检查证据
- **修改内容** · `tests/test_ui_fonts.py`（21 → 23 用例） — Windows 专属红项修在测试侧：夹具名含 `:` 会被解析成名为 `font` 的备用数据流（`exists()` 为真、`shutil.copy2` 抛 WinError 3），故把字符清洗下沉为纯函数用例、fs 用例改用两端都能创建但仍需清洗的名字；另补「保留设备名清洗」与「失败必须留一行带异常类型名的 warning」两条
- **说明** · 全量 `pytest` — 4345 passed / 14 skipped / 0 failed / warnings summary 为空。总数比上一轮 +12：+4 是本轮两条新锁的用例，**+8 来自 `tests/test_test_hygiene.py` 的 4 条按 `tests/` 目录参数化的检查**各多访问两个新文件（采集面变化，不是新增断言）

### 11. 文档与知识资产（`AGENTS.md` / `CODE_WIKI*` / `README*` / `docs/agent-reference/`）

- **修改内容** · `AGENTS.md` — 补登记四条：i18n 盲区⑤（raise 位必须在产生点接 `tr()`）、Qt 构造标签与 `T()` 键必须同 msgid（第 ⑦ 条）、自选字体第 ④ 条（失败必留 warning + 保留设备名清洗）、缺键补写必须幂等；另立「引用代码位置优先写锚点而非 `file:line`」新约定
- **修改内容** · `AGENTS.md` — 事实改正 6 处：设计令牌 26 → **25** 键 × 3 主题、`record_danmaku_args` 52 → **60** 处赋值、三处失效 `file:line` 引用改锚点（`main.py:3595`、`Dockerfile:112`、`src/runtime/ffmpeg.py:583`）、black 与 mypy 的文件计数不再当常量引用（两个口径互不相等）
- **修改内容** · `AGENTS.md`、`docs/agent-reference/measured-evidence.md` — 精简采取外迁 + 合并而非删除：五组一次性实测读数逐字搬入参考文档（19 341 → 24 042 B），根侧留约束与指针；两条挂在同一标题下的条目按内容分标题、DoD 第 5 步改为指针、两处 `[历史注]` 压成一行。根文件体积 157 364 → 158 266 B（净增，因为同轮新增了两条长期约定）
- **修改内容** · `README.md`、`README_EN.md` — 〔配置〕配置块默认值与 `config/config.ini`、代码 fallback 逐项对账同步；版本条目保持中英一一对应
- **修改内容** · `docs/agent-reference/project-structure.md`、`docs/agent-reference/session-learnings.md` — 目录树同步新增模块与测试文件；经验条目追加（ADS 夹具、保留设备名实测、用例数增量归因、offscreen 探针的退出形态、先测后改的冗余度量、锚点优于行号、`strict=True` 下的重复键）
- **新增功能** · `CODE_WIKI.md`、`CODE_WIKI_EN.md` — 本「按模块分类的改动总览」章节（中英同结构、同条目数、逐条对应），以及逐批更新日志条目随改动追加并保持双语一一对应
