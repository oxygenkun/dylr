# 更新日志

[返回 README](../../README.md) · [English](release-notes-en.md)

## v4.5.0 (2026-10-02 ~ 2026-10-17) — GUI 窗口层迁移 PySide6（Qt6）与界面现代化阶段4~6 收尾 / 移动端与短链平台接入（b23.tv / m.douyu.com / hy.fan / 小红书四类归一）/ Linux arm64 ffmpeg 与 GHCR 多架构镜像 / 启动期字节码缓存清理与发布链在线来源层 / config.toml 重复键容错读取层与 i18n 三批机检 / 全工作区代码审查与两轮元数据对账

> 本版本（v4.5.0，2026-10-02 ~ 10-17）是一次「GUI 全面现代化 + 平台接入扩张」周期：① **窗口层由 tkinter/ttk 迁到 PySide6（Qt6）**，新增 `src/gui_qt/`，令牌与 QSS 单一事实源、控制器层可离屏单测，打包侧靠 `QT_BLOAT_FILES` 剪除 Qt 死重；② **界面现代化阶段4~6 收尾**（CTk → ttk、`customtkinter` 摘除、字号 / 缩放 / 动效）；③ **平台接入扩张**（三类移动端短链 + 小红书四类链接归一，Linux arm64 与 GHCR 多架构补齐）；④ **config.toml 同节重复键不再整份失效**（新增 `src/config/reader.py`）。**存在破坏性变更（GUI 实现与依赖面）**，见下方专节。详细根因与验证见 [CODE_WIKI.md](../../CODE_WIKI.md)。

**✨ 新增功能 / 改进**
- **GUI 窗口层迁移 PySide6（Qt6）**：新增 `src/gui_qt/`（16 文件 4189 行，含 `pages/` 五页），`gui.py:main()` 委托 `src.gui_qt.app.main()`（tkinter 实现留作 `_legacy_main()`）。令牌与 QSS 单一事实源在 `theme.py`，线程模型换轨 Qt Signal/Slot + `QTimer`；控制器层无 Qt 布局代码，`QT_QPA_PLATFORM=offscreen` 即可离屏单测。
- **界面现代化阶段6（字号 / 缩放 / 动效）**：新增 `src/ui_scale.py`（语义字号七档 caption 10 → display 28、间距栅格、缩放档位、小屏自动降档）与 `src/ui_motion.py`（off/subtle/full，系统「关闭动画」优先）。缩放只改字号与间距，点值按 `factor ** 0.6` 温和增益（等宽再乘 0.9）。
- **GUI 视觉语言改版（品牌蓝 → 暖赭橙）**：`src/ui_theme.py` light/dark 两表换 16 个槽位，`gui.py` 的 `Colors` 同批换值；主色 `#B5573A`（白字 4.78；原型 `#C96442` 实测 3.90 不达 AA），深色 hover `#A8502F`。`high_contrast`、`TERMINAL_*` 与语义色槽未动。
- **GUI 自选字体**：新增 `src/ui_fonts.py`（SFNT name 表解析 + GDI 私有字体注册，拒收 woff2）；切族在同一 `configure` 完成、不重建控件树。可用性判据是让 Tk 解析一次并与缺失族名的兜底结果比对（`AddFontResourceExW(FR_PRIVATE)` 成功但 `tkfont.families()` 列不到私有字体）。
- **GUI 静态标签接入 i18n**：新增 `src/ui_i18n.py`（`T()` + `retranslate_all` + `apply_title`），静态标签 54 处、运行时串约 60 处接线，语言切换整窗回填不重建；`src/api/routes.py` 27 处展示类文案经 `tr()`（稳定机器码 detail 不翻）。
- **移动端与短链平台接入**：B站 `b23.tv`（纯数字直取 + 短码 302 跟随）、斗鱼 `m.douyu.com`（白名单 + 房间号归一）、虎牙 `hy.fan`（新 resolver + 301 跳转归一）、小红书 `parse_xhs_live_url`（`/livestream/<房间ID>` / 动态路径 / `xhslink` / 分享参数四类）。归一后复用既有取流链，`record_host` 仍取短链域以便熔断分桶计数。
- **Linux arm64（aarch64）ffmpeg 三段落地**：发布矩阵新增 `ubuntu-24.04-arm`（四平台 × lite/full = 8 zip）；新增 `src/ffmpeg_linux_download.py`（官方 digest 优先、TOFU 降级、两趟安装）作无 root 兜底；新增 `docker-publish.yml`，原生双 runner 各出一架构层并按 digest 合成 manifest（仅 `v*` tag）。
- **启动期字节码缓存清理**：新增 `src/app/startup_cleanup.py`——正向白名单扫描根、基线 = `sha256(相对路径 + 字节)` + 版本号、落 `logs/.pyc_stamp`，只在源码或版本变过时回收一次（默认开，可关）。
- **发布链运行时钉定「在线来源层」**：`tool/build_exe.py` 新增三态开关 `DLR_RUNTIME_PIN_SOURCE`（off/verify/adopt，默认 off 不发请求）；`adopt` 仅对「清单独立认证」（`trust=signed` 且验签通过）开放，在线值须过 64 位小写十六进制形状关。`_PINNED_RUNTIME_SHA256` 未改，默认行为与引入前一致。

**🐛 修复的问题**
- **config.toml 同节重复键让整份配置失效**：`[录制设置]` 同名键落两行时 `RawConfigParser()`（strict 默认 True）抛 `DuplicateOptionError`，一个键都读不出（`import main` 即崩、GUI 每 10s 报错）。新增 `src/config/reader.py`（`tolerant_config_parser` / `find_duplicate_options` / `warn_duplicate_options`），接线 10 处读取点，缺键补写保持幂等。
- **Qt 等宽字体族名告警**：`QFont` 族名位只收单个 family，`QFont("Consolas, Menlo, Courier New, monospace")` 在未装 Consolas 的机器按 `styleHint=Monospace`（枚举 7）落进 `Terminal`，打出 `CreateFontFaceFromHDC() failed` 与 `setPointSize(-1)`。新增 `fixed_font()` 按候选取本机首个命中族名。
- **`src/ui_fonts.py` 两处缺陷**：① 复制失败被 `except Exception: return None` 吞掉且零日志 → 先落带 `{type_name}` / `{err}` 的 warning；② `_safe_filename` 不处理 Windows 保留设备名（`CON`/`PRN`/`AUX`/`NUL`/`COM1-9`/`LPT1-9`），按首个点之前的基名大小写不敏感比对并加 `_` 前缀。
- **GUI 右边框裁切**：三条长提示初始 `wraplength=0` 把 `winfo_reqwidth()` 撑到 1248 > geometry 1120，而 grid 对「需求宽 > 分配宽」是裁切而非收缩。修法三段：抬首窗几何消首帧赤字、压到档位最小宽读出「收缩地板」后只抬 `minsize` 到地板、尺寸 clamp 到 `SCREEN_FILL_CEILING`。
- **小红书离线轮拿不到主播名**：改读 `liveStream.roomData.hostInfo.nickName`（实测 `liveStatus="end"` 时仍存在）；旧实现只在开播态抠 `roomInfo.deeplink`，等待开播的每一轮都误判失败，取不到时用 `小红书_<room_id>` 兜底。
- **斗鱼房间号提取两个边界洞**：旧正则未锚定参数边界（`?xyzrid=123` 误命中）且把尾斜杠与后续路径带进房间号（`/8751648/`）。改走 `urlsplit` 在解析层剥掉 scheme / 尾斜杠 / query / fragment；错误提示为 `i18n.tr` 常量模板且链接先过 `mask_credentials()`。
- **`CODE_REVIEW_2026-10-02` 整改收尾**：补 L-04（`shopee` / `花椒直播` 缺 `platform_cookie` 映射）与 L-05（删模块级死全局）；修正整改自引入的 4 类缺陷——`except Exception:` 仍引用 `type(e)` 的 `NameError`、`mask_credentials` 收 tuple 的 `TypeError`、日志刷新链复位点，以及裸真值把 `"0"` 误判私有（改走 `parse_config_bool`）。
- **i18n 三处**：Qt 侧栏 5 条「按名 `tr()`」msgid 缺失（Qt 用两空格形态、目录只有三空格形态）→ 四目录各 +5 条并存；9 处 Qt 按钮图标被 `set_text` 打回裸文本 → 构造标签与 `T()` 同 msgid、复用既有键（零新增）；21 处中文 raise 文案在产生点接 `tr()`（去重 16 条 msgid）。
- **签名脚本钉定表尾字节漂移**：pre-commit 的 `end-of-file-fixer` 给 `src/javascript/` 下 4 份脚本各追加换行，而 `_check_js_hash` 按原始字节求 SHA256，四条全失配；按拍板重算钉定表（`migu.js` 本就带尾换行，未动）。

**⚠️ 破坏性变更 / 行为变化**
- **`customtkinter` 整体摘除（阶段4~5）**：控件层 CTk → ttk，新增 `src/ui_kit.py`（六个兼容工厂 + `DataTable`），配色由 `ui_theme` 注册的 ttk 变体派生、「界面主题」成为唯一明暗入口；`CTkToplevel` → `tk.Toplevel`、`CTkFont` → 负数点值 `tkfont.Font`，存根删除。兼容契约逐字保持（`import gui` 无头导入、录制链一行不动）。
- **GUI 窗口层换轨 PySide6**：`gui.py:main()` 委托 `src.gui_qt.app.main()`，tkinter 实现留作 `_legacy_main()`；`packages` 增列 `src.gui_qt` 与 `src.gui_qt.pages`（显式 packages 不递归发现，漏列会让 `douyin-recorder-gui` 入口 `ModuleNotFoundError`）。
- **镜像默认不装 GUI 依赖**：`Dockerfile` 新增 `ARG INSTALL_GUI=false`——阶段1 按行首剔除 `pyside6` 后再装，阶段2 仅 true 时补 Qt 运行库；依据是「PySide6 解压 647.56MB，屏蔽后 `import main` / `import web` 仍成功」。过滤只在镜像侧，compose 的 `gui` 服务另用 `:latest-gui` tag。
- **`[tool.mypy].exclude` 目录名锚定**：该字段是路径子串正则，裸 `"ffmpeg"` / `"node"` 曾把 12 个文件（`src/ffmpeg_*`、`tests/test_ffmpeg_*`、`node_install`）排除在无参 mypy 检查外；改 `(^|/)ffmpeg/` 与 `(^|/)node/` 后首次进检查并暴露 8 处存量标注错误（已修）。
- **CI 真窗用例由 skip 转为真实执行**：test job 的 pytest 改 `xvfb-run -a` 包裹并加装 xvfb，另加「Tk 开显示」兜底步骤（xvfb 失效当场红，防真窗用例静默消失）。

**📦 依赖变更**
- **运行时依赖新增 `PySide6>=6.12.0`**：随窗口层迁移加入（摘除 `customtkinter` 后 20 条 → 21 条），下界 6.12 为实测支持 Python 3.14.8 的最小版；`uv.lock` 与 egg-info 经 `sync_metadata.py` 重生成。`pystray` / `Pillow` 保留（Web 托盘 `src/web_tray.py` 仍走 pystray）。
- **移除 `customtkinter`**：`requirements.txt`、`pyproject.toml`（运行时清单 + `.[gui]`）、`typings/customtkinter/` 同批摘除；`tool/build_exe.py` 移除 `collect_data_files('customtkinter')`，excludes 条目按防御口径保留。
- **dev extra 新增 `pre-commit>=3.6.0`**（配套 `.pre-commit-config.yaml`）；属 dev extra、不入 `requirements.txt`，运行时依赖仍 21 条。
- **CI 工具版本常量对齐本地实装**：`isort_version` 9.0.2、`black_version` 26.10.1，`.pre-commit-config.yaml` 两个 mirror rev → 26.10.1 / 9.0.2，使「本地 venv / CI / 提交钩子」同值；抬值前两版 black 各跑一次全量 `--check`（判定不变），新 rev 逐个经 GitHub API 核实。

**🛠️ 仓库维护与文档**
- **界面现代化阶段4~6 收尾**：CTk → ttk 控件层、依赖摘除与打包清理、体积基线补齐（91.81MB / 224 文件）、xvfb 常态化，以及字号 / 缩放 / 动效。
- **i18n 机检三批**：新增 `tests/test_i18n_catalog_coverage.py`（按名调用点 msgid 四目录齐全 / 白名单集合相等 / 键集合逐项相等）、`test_i18n_raise_site_tr.py`、`test_gui_qt_label_parity.py`；`extract_i18n_strings.py` 认识 `T(...)`。四目录键数 1110 → 1127（期间收回 6 条孤儿键）。
- **检查与 CI**：typecheck 补 `mypy --platform win32`；新增 `pyrightconfig.json`（include 等于 `[tool.mypy].files`、exclude 覆盖两份清单），使不带路径跑 `pyright` 复现 IDE 扫描面；新增 `tests/test_ci_frontend_registration.py`（`.mjs` ↔ ci.yml 清单双向差集）与 Hook 执行体 `tool/agent_hook_guard.py` + `/dlr-dod`。
- **代码审查与元数据对账**：`CODE_REVIEW_2026-10-02.md`（8 工作包深审，严重 2 / 中等 18 / 轻微 47 = 67；报告矩阵原记 17 / 66 已按枚举更正）；两轮元数据同源对账（取值零改动）；`AGENTS.md` 69 项机检、6 处断言按实测改正、3 处 `file:line` 改锚点。
- **CODE_WIKI 双语新增「按模块分类的改动总览」**（11 个小节，同一张数据表渲染）；Linux arm64 与四平台矩阵同步进双语 README 的产物清单与 GHCR 拉取说明。

**🧪 测试与验证**
- 全量 `pytest` **4365 passed / 14 skipped / 0 failed、warnings 空**；`run_gates.py` **10/10 全绿 rc=0**（black 246 文件、无参 mypy 245 文件 + linux/win32 双跑）。
- 覆盖率总 **83.10%**，`check_coverage.py` **69 模块全达标**（`src/config/reader.py` 98%）；basedpyright 与裸跑 `pyright` 均 0 errors / 0 warnings。
- 打包体积 **153.21MB / 359 文件**；`QT_BLOAT_FILES` 11 项 / 39.25MB 在 COLLECT 后按文件名剪除（excludes 管不到 hook 收的 DLL），冻结 GUI exe 实跑 9s 存活。
- 真机：虎牙 660000（2 条 / 206 B）、B站 545068（33 / 2642 B）、b23.tv 22747736（18 / 1402 B）均 PASS；`hy.fan/JbmwoV` 真实 301 解析到 `www.huya.com/30764624`（在播，该时段无弹幕记 WARN）；小红书 570480456678837366 解析 OK（`liveStatus=end`）；斗鱼 `m.douyu.com/5526219` 完整取流链返回 `flv_url` / `m3u8_url` / `record_url`。
- GUI 真窗：主题 ×4 / 缩放 ×5 / 动效 ×4 / 五页面 / 对话框 / 高级设置全 `ALL_OK`，85% / 100% / 150% 三档截图复核（「按钮内边距不跟随」「侧栏底部溢出」「页面滑入后消失」三缺陷即在此发现）。

## v4.4.0 (2026-09-29 ~ 2026-10-01) — Web 面板 FastAPI → Starlette 迁移（移除 fastapi/pydantic，24 条路由契约逐字保留）/ Web 前端动效层与移动端适配 / GUI 主题层（阶段3）与长文案自适应折行根除 DPI 卡死 / 抖音原画 HEVC 房「无可用源」死锁根治与 h265 候选按保存格式放行 / 两轮全量代码审查与 P0 修复（standalone SSRF 三道防线、ffmpeg 命令日志脱敏等）/ i18n 盲区补录 / 依赖与文档多轮对账

> 本版本（v4.4.0，2026-09-29 ~ 10-01）为一次「UI 现代化 + 全量审查修复」周期。四点最值得注意：① **Web 后端从 FastAPI 迁移到 Starlette 直接驱动**——自研路由适配器与零依赖校验层，24 条路由契约与全部安全不变量逐字保留，运行时依赖 23 → 21；② **两轮全量代码审查**（`CODE_REVIEW_2026-09-29_2` 31 编号全落地 + `CODE_REVIEW_2026-09-30` 严重 2 / 中等 51 / 轻微 160、P0 已修）清掉 standalone 独立发行版的 SSRF/本地文件读取链、ffmpeg 命令凭据明文落盘等一批静默失效形态；③ **抖音原画 HEVC 房死锁根治**——主播在播却每轮「本轮无可用源」永不录制的死锁（hevc 替换、h265 候选剔除、HLS 静默丢弃三规则叠加）已修复，h265 候选按保存格式（TS/MKV/MP4）放行直拷并新增零源观测告警；④ **GUI 主题层（阶段3）** 与 **Web 前端动效层（阶段1）** 落地，GUI 长文案自适应折行根除两侧裁切与 DPI 重缩放卡死。**存在破坏性变更（依赖面）**，见下方专节。详细根因与验证见 [CODE_WIKI.md](../../CODE_WIKI.md)。

**✨ 新增功能 / 改进**
- **GUI 主题层（阶段3）**：新增 `src/ui_theme.py`（零显示依赖、可无头 import）——13 个语义槽位 × 三套主题（浅色 / 深色 / 高对比度），侧边栏「界面主题」菜单运行时切换并持久化到 `config.toml [GUI] gui_theme`；WCAG 对比度机检（正文 4.5 / 非文本与禁用 3.0）由 `tests/test_ui_theme.py`（34 用例）持续回归。
- **Web 前端动效层（阶段1）**：新增 `web/motion.js`（零依赖 IIFE，挂载 `window.__dlrMotion`）——滚动入场（IntersectionObserver 错峰）/ 视差粒子（数量按视口自适应、低核设备减半）/ `prefers-reduced-motion` 全降级 / 页面隐藏暂停 / `destroy()` 无残留定时器；`web/index.html` + `web/style.css` 接线；`tests/frontend/test_motion.mjs` 5 用例（node:test 沙箱，零 npm 依赖）。
- **Web 后端自研适配层**（随迁移新增）：`src/api/models.py`（9 个纯标准库 dataclass 请求模型，替代 pydantic）+ `src/api/routes.py::_route` 路由适配器（JSON body 解析 / Query 上下界夹取 / 同步端点线程池派发 / JSONResponse 包装），24 个路由处理器函数体零改动。
- **`retry` 复合动作新增 `fail_fast_codes` 入参（按退出码分档）**：不传时既有 16 个调用点行为逐字不变；web smoke 调用点传 `"2"`，配置类错误立即失败不进入退避——不得为了流水线变绿退 0 或降级 warning。
- **两道新测试防线**：R7 真机脚本模板 AST 检查（`__main__` 守卫 / 收集期零副作用 / argv 两步守卫 / 按平台前缀清理，5 个 live_collector 同批加固）；R8 全仓 `MUTATION-` 残留扫描（变异验证未还原即判红，配套三条硬约束写回 AGENTS.md）。
- **新增动态路由契约锁** `tests/test_web_api_routes.py`（24 条 method/path 精确集合 + `/web` 挂载点），取代被敏感检查拦截的静态基线 JSON。
- **h265 候选按保存格式放行（TS/MKV/MP4 可直拷 HEVC）+ HLS 静默丢弃的零源观测告警**：`_h265_copy_format_supported()` 按 main 热更新的 `video_save_type` 判定容器能否直拷 HEVC——TS/MKV/MP4 放行（h265 主候选进常规探针序列）、FLV（HEVC-in-FLV 属 Enhanced-FLV 扩展）/MP3/M4A/域外取值保守剔除；main.py 的 h265→TS 强制块加同一口径守卫（保存格式已是 TS/MKV/MP4 时不再接管、不再打「use TS format instead」告警）。m3u8 候选被 HLS 配置（全局开关/排除平台）整组静默剔除且本轮最终无可用源时，新增点名成因、候选条数与恢复开关指引的观测告警——补上此前唯一「零日志」的选源成因；选中源的健康轮不打。

**🐛 修复的问题**
- **抖音原画 HEVC 房「本轮无可用源」死锁根治**：主播在播却每轮跳过录制，状态行累计错误数恒 0（选源失败轮不计错误样本，纯看状态行会误判正常）。根因是三条各自成立的规则叠加——原画请求且接口下发 `hevc_flv_url` 时 h265 FLV **替换** h264 成为唯一 FLV 候选；`select_source_url` 把一切 `codec=h265` 候选在探针前剔除；HLS 采集关闭时 h264 m3u8 组被静默剔除、record_url（`.m3u8`）被联动禁用——过滤后零可用候选。修复：hevc 替换发生前把被替换的 h264 原画地址存入 `flv_url_list`（HLS 关闭/不可达时落到该备选；与 hevc 地址逐字相同或自带 `codec=h265` 标记的条目不收）。真机验证 hevc 原画在播房：HLS 关闭态选中 h264 FLV 备选（修复前该形态恒「本轮无可用源」）→ `E2E_RESULT: CURED`。
- **standalone 独立发行版孪生漂移（严重 2 项）**：① 探针/录制链 SSRF 与本地文件读取链（默认 `build_opener` 注册 FileHandler + 候选无协议/内网判定 + ffmpeg 缺 `-protocol_whitelist` 三层叠加，`file://` 与云元数据地址可被「探测→放行→录制落盘」完整走通）——补显式 Handler 白名单、`_untrusted_stream_target_reason` 内网判定（含 `2130706433`/`0x7f000001` 等缩写形态）与两处 `-protocol_whitelist`；② 完整 ffmpeg 命令（含 Cookie 头与带 token 流地址）逐轮明文落 `logs/ffmpeg.log`——写入前经 `_mask_ffmpeg_cmd_for_log` 脱敏。
- **P0 六项**：磁盘满暂停期退出的房间不再永久残留 `running_list`（空间恢复后全部房间不再拉起的根因）；GUI 保存配置改真原子写（POSIX 下不再把 0600 收紧还原成 umask 权限）；Web 口令 strip 三处统一（首尾带空格的口令不再认证自锁 403）；`update_config_line` 移除无引号值行内注释回落启发式（含 ` #` 的配置值不再被静默污染）；header 形态驼峰凭据（`myToken:`/`sessionKey:` 等）恢复脱敏；`replace_url` 改段级精确匹配（URL 前缀重叠的兄弟房间行不再被「地址失效自动注释」整行误伤）。
- **全量审查修复 31 编号**：Shopee 短链落地页双闸（域族白名单 + 内网判定，不可信即丢弃跳转、剥离 Cookie）；异步 HTTP **逐跳**重定向内网复检；`sync_req` scheme 白名单 opener（不再注册 file/ftp/data handler）；PandaTV/WinkTV 等平台进日志的原始 URL 一律脱敏；快手 did / B站 buvid 跨代理出口不再串用设备指纹；`anchor_name` 为 None 不再崩掉整轮解析；TikTok HLS-only 房间「选流畅实拉原画」且无降级提示的选档缺陷；B站弹幕 host 轮换「假关闭」吞真实断连；Web 认证拒绝文案不再被 stdio 重定向吞掉；`tr()` 二次异常不再顶掉原始异常；前端 reauth 口令改掩码弹窗、三条轮询链代次令牌防失联定时器；GUI 状态行匹配串按语言惰性重算、`after` 自续期链 `try/finally` 防永久断裂、「停止录制/彻底退出」共用单飞入口、tail 线程逐事件容错；真机脚本收集期真连平台/清空输出目录的会话污染（`pytest --collect-only` 20.55 秒 → 0.81 秒）。
- **同步探针内网收口**：同步探针此前连初始 URL 的内网/回环/云元数据判定都没有（协议形态白名单挡得住 `file://`、挡不住 `http://127.0.0.1:6379`）——`_probe_client()` 单点工厂 + 发请求前初始判定 + `RedirectHopRejected` 收敛；实测公网 HLS 走通、`127.0.0.1:6379`/`169.254.169.254`/`10.0.0.5`/CGNAT 全拒（末位候选也不放行）。
- **GUI 长文案两侧裁切与 DPI 卡死（同根因两批修复）**：`_bind_adaptive_wraplength` 自适应折行根除 pack 两侧对称裁切（5 处同形态一并修复）；随后把 `<Configure>` 同步追写改为「真防抖（风暴安静 120ms 才结算）+ 迟滞（|Δ| ≤ max(12, 2%) 不写）+ TclError 竞态守护」，根除与 CTk DPI 重缩放递归互泵的事件风暴（整窗卡死 / 文字高频闪烁 / 元素渲染不完整）。
- **Web 面板移动端适配**：顶栏两行化（固定 56px → min-height + ≤768px 换行）、`viewport-fit=cover` + `env(safe-area-inset-*)` 安全区适配、`100dvh` 动态视口、三张数据表窄屏面板内横向滚动——消除 iPhone 16 Pro Max / Pixel 10 上的顶栏裁切与整页横向滚动。
- **i18n 盲区补录**：提取器三类盲区（`print_colored` / `messagebox` / 推送正文）的用户可见文案全部 tr 化，四语目录登记 16 条新词条并重编 `.mo`；zh_TW 顺带去重 7 条重复键，四目录键集恢复严格一致（808 键；10-01 随 h265 零源观测告警词条再登记 1 条，终态 809 键）。

**⚠️ 破坏性变更 / 行为变化**
- **移除 `fastapi` / `pydantic` 运行时依赖**（明细见下方「依赖变更」）：Web 面板的 24 条路由、请求/响应形态、安全头与鉴权中间件行为不变；内部请求模型改 `.parse()` 读取，对外部使用方无 API 面变化。仅在面板之外直接 import 这两个包的自定义部署需要自行调整。

**📦 依赖变更**
- **运行时依赖 23 → 21 条**：删除 `fastapi>=0.140.0` 与 `pydantic>=2.13.4`；`requirements.txt` 与 `pyproject.toml [project.dependencies]` 包名集合逐项相等（回归锁 `tests/test_regression_2026_09_22_gates.py`），`uv.lock` 与 `DouyinLiveRecorder.egg-info` 经 `tool/sync_metadata.py` 重建（二包零残留）。
- **`urllib3` 声明下限 2.7.0 → 2.8.0**：CI `deps-audit`「下限复核」实测旧下限自身落在 CVE-2026-97687 / CVE-2026-97688 / CVE-2026-97689 受影响段（修复版本均为 2.8.0）；`requirements.txt` 与 `pyproject.toml [project.dependencies]` 同步抬升（仍 21 条、包名集合不变），`uv.lock` 与 `DouyinLiveRecorder.egg-info` 经 `sync_metadata.py` 重建；本机 venv 已装 2.8.0，解析集合不变。
- **CI typecheck job 补装固定版本 `pytest==9.1.1`**：`tests/` 的类型检查不再半盲（fixture 与 `pytest.fail()` 的 `NoReturn` 恢复可见）。
- **文档依赖口径修复**：双语 README「安装开发依赖」pip 行由 5 条（缺 `pytest-cov`，照此装出的环境跑不了覆盖率检查）改为 `pip install .[dev]` 单源形态（指向 `[project.optional-dependencies].dev` 六条）。

**🛠️ 仓库维护与文档**
- **两轮全量代码审查**：`docs/worklog/CODE_REVIEW_2026-09-29_2.md`（31 编号按 P0+P1+P2 全落地）与根目录 `CODE_REVIEW_2026-09-30.md`（18 批逐文件深审约 42,000 行生产代码 + 56,200 行测试，严重 2 / 中等 51 / 轻微 160，Mimosa 深扫交叉证据）；P0 已落地，P1/P2 待后续批次，standalone「回灌对齐」建议单独立项。
- **`AGENTS.md` 多轮对账与精简**：7 项事实同步（用例数 / 符号名 / 调用点数等旧读数更正）+ 信息保真精简 19 处（110,287 → 108,469 字节；检查 bash 块与全部章节标题逐字未动，三道文档锁 73 passed / 1 skipped 不变）+ 三条检查口径修正（质量检查判定改为引用「格式化命令（检查唯一基准）」一节、`pyright`/Pylance 定位厘清为非检查、新增「本地检查可外推前提：工具版本须与 `ci.yml` 钉定值同值」）；stop-hook 凭据风险提示经核查为误报后按建议把红线措辞显式化。
- **元数据对账**：15 个文件对照 `pyproject.toml` 全量核对（版本 4.4.0 / 21 条依赖 / 镜像与服务名 / 端口与挂载 / 打包参数），唯一漂移即上述 README dev-deps 行；`pyproject` `filterwarnings` 两条注释归因由「fastapi testclient」更正为 `starlette.testclient` 本体。
- **两处测试面修复（无生产代码改动）**：`test_gui_stop_exit_singleflight` 在 Linux CI 的 4 条假红系测试替身只桩 win32 信号分支（POSIX 走 `os.kill`），按「stdlib 替身走被测模块命名空间 shim」口径补打桩后，伪造 `sys.platform` 本地复现 POSIX 路径、四场景 25 项判据全过；`test_notify` 超时用例的墙钟余量改由生产常量推导（`taskkill /F /PID` 实测稳定 2.5~3.1s，1s 超时 + ~3s 恰撞 4.0s 手拍余量的机器态误报根除）。

**🧪 测试与验证**
- 全量 `pytest` **3721 passed / 14 skipped / 0 failed / 0 警告**（2026-10-01 终态实测；14 条 skip 均为平台条件性：符号链接特权缺失、Windows chmod 只读位语义、SIGKILL POSIX 形态、h2 已装致缺依赖分支不可达等）。
- 覆盖率 44 模块全部达阈（总 84.9%，coverage.json 实测）；basedpyright 0 error / 0 warning；`run_gates.py` 8/8 全绿。
- 真机验证（09-29 ~ 09-30）：抖音 PASS（59 条 / SRT 4518 字节）、B站 PASS（9 条 / 678 字节）、虎牙 WARN（连接正常、该时段无弹幕）、Twitch WARN、斗鱼 SKIP(房间未开播，解析链正常)、TikTok SKIP(无出境网络)；standalone `--dry-run` 虎牙真实在播房间 PASS（S-01/S-02 增量验证）；公网 HLS 分片探针走通且四类内网目标全拒。GUI 目视项、斗鱼/TikTok 活房间复跑、打包体积复测等交回动作见 [CODE_WIKI.md](../../CODE_WIKI.md)。
- 真机验证（10-01，抖音 hevc 原画在播房「央视网快看」）三态全 PASS：HLS 关 + TS 保存格式 → h265 FLV 主候选被放行并选中（codec=h265 实测探针通过）；HLS 关 + FLV → 落到 h264 原画备选（codec=h264）；摘除备选 → 返回 None 且零源观测告警实弹打出。

## v4.3.0 (2026-09-16 ~ 2026-09-27) — P0 修复 ffmpeg master 构建下录制 100% 失败（`-thread_queue_size` 被上游收窄为输出专属选项）/ 布尔配置解析口径统一（`true/false` 致 8 项配置静默失效、9 个海外平台无法录制）/ 两轮全量代码审查（62 + 106 项分级修复）/ 供应链加固（官方哈希优先 + GPG 验签 + 删除蓝奏云兜底）/ 发布链四处故障修复（**对外分发的 lite 包一度从未真正存在**）/ Apple Silicon 的 ffmpeg PATH 让位策略 / 日志房间关联字段与 `/health` 探活端点 / 构建产物体积 −21.6% / 覆盖率 73.28% → 82.03%（专项）与 83.91%（09-27 终态）

> 本版本（v4.3.0，2026-09-16 ~ 09-27）为一次以「正确性 + 供应链安全」为主线的收敛周期。四处最值得注意：① **P0**：ffmpeg master 构建把 `-thread_queue_size` 收窄为输出专属选项，我们仍放在 `-i` 之前，导致该构建下**每一次录制都以 `-22 (EINVAL)` 退出、零字节产物**；② **布尔配置解析口径统一**：`config.toml` 写 `true/false` 曾被判为无效并**静默**回落硬编码兜底值（无告警无日志），实测致 8 项配置生效值漂移，其中最严重的一项使 9 个海外平台 100% 无法录制，现已统一为「`是/否` 与 `true/false`/`1/0`/`yes/no`/`on/off` 等价」；③ **两轮全量代码审查**（09-19 的 62 项：严重 12 / 中等 22 / 轻微 28；09-21 的 `CODE_REVIEW_2026-09-20` 106 项：严重 10 / 中等 70 / 轻微 26）把 SSRF、面板接管、凭据明文落盘、弹幕线程死循环等一批静默失效形态一次性清掉；④ **发布链四处故障**（09-26 ~ 09-27），其中最影响用户的一条是：`make_zip` 用 `Path.with_suffix(".zip")` 收尾，而版本号本身带点，lite 与 full 算出同一个附件名，第二次以覆盖模式原地截断第一次，**某次 Release 实际只留下一个既无平台也无变体标识的包，精简版从未分发出去**。**存在破坏性变更**，见下方专节。详细根因与验证见 [CODE_WIKI.md](../../CODE_WIKI.md)。

**🐛 修复的问题**
- **P0：ffmpeg master 构建下录制 100% 失败**：`-thread_queue_size` 被上游收窄为 muxer 专属选项，置于 `-i` 之前时 ffmpeg 以 `Option thread_queue_size … cannot be applied to input url … Error opening input files: Invalid argument`（返回码 -22）退出；与房间 / 平台 / CDN 无关，该构建下每次录制都失败。
- **P0：布尔配置解析口径统一**：散落四处的解析（原 `!= "否"` / `== "是"` / 前端 `=== '是'`）统一到 `src/config/booleans.py::parse_config_bool`；修复后 `global_proxy` 不再被错误置 False，TikTok / SOOP / PandaTV / WinkTV / Flextv / PopkonTV / Twitch / LiveMe / Faceit 等 9 个海外平台恢复解析。
- **全量代码审查 62 项（09-19）**：GUI 空配置启动静默卡死；配置行多一个逗号致其后所有房间永久不录；Tars 解码缺边界校验致弹幕线程死循环；斗鱼弹幕被静默过滤（已修复项回归）；ffmpeg 挂起永久占用并发槽；零字节产物被判成功并撤销线路退避；`config.toml` 含凭据被明文复制到 `backup_config/`；Web 面板可被自身接口接管或锁死；房间 URL 零协议校验 → 盲 SSRF；脱敏黑名单漏掉「凭据嵌在值里」的 URL 型键；安装与打包链路零完整性校验。
- **`CODE_REVIEW_2026-09-20` 全轮落地 106 项（09-21）**：并发信号量把「可用许可数」当容量（网络并发上限实质失控）；`PUT /api/rooms` 绕过房间入口校验；内网地址拦截用字符串前缀黑名单（实测 7 条放行 5 条）；面板认证可被单个写请求热关闭、大小写变体绕过口令哈希 / 防自锁 / token 吊销；CRLF 配置下删除房间永久静默失效却回报 `{"ok": true}`；Shopee 站点后缀解析拼出非法域名；兜底装饰器与返回契约错配（故障伪装「未开播」）；录制看门狗「停滞」判据基准用错（容忍窗口实际只有 30 秒）；`only_flv` 分支缺 `flv_url` 致时间字幕线程死循环写盘；发布链运行时二进制哈希钉定表为空。
- **发布链与供应链**：macOS arm64 的 full 包**静默不含 ffmpeg**（上游无该产物，下载点已修复）；Windows 运行期 ffmpeg 主源经实测确认健康（gyan.dev 带 `.sha256` 文档，与发布期钉定互证）；官方 SHA256 钉定值回填 6/10 槽。
- **测试卫生（09-23）**：一处跨文件补丁泄漏（漏掉的 `undo()`）让同会话后续用例发出真实网络请求，造成 13 条失败里 11 条「全量红、单跑绿」的假失败。
- **P0：构建脚本污染测试会话（09-23）**：`build_exe._ensure_utf8_streams()` 裸 `reconfigure` 破坏 pytest 的 fd 捕获，表现为「GUI 启动失败」弹窗与无关用例被判 FAILED/ERROR（九个 CI job 全为 ubuntu，恒绿不复现）。
- **发布链四处故障（09-26 ~ 09-27，对外产物受影响）**：① 钉定表八个槽位被误改成「官方签名档」标记，`check_runtime_pins.py --strict` 在 prepare 阶段即 rc=1，发布根本走不到构建（已按官方通道回填哈希）；② Linux 内置 ffmpeg 的钉定值取自 BtbN 的滚动 `latest` 别名，上游同日重发同名资产即换 digest，发布链当天红在 SHA256 校验（改钉**月末不可变 release 标签** `autobuild-2026-08-31-13-27`，URL 与 digest 双不可变）；③ **lite 精简包从未真正分发出去**：`make_zip` 以 `Path.with_suffix(".zip")` 收尾，而版本号 `4.3.0` 自带点，`DouyinLiveRecorder-v4.3.0-windows-amd64-lite` 与 `-full` 被截成同一个 `DouyinLiveRecorder-v4.3.zip`，第二次以覆盖模式原地截断第一次且不报错，Release 附件里只剩一个既无平台也无变体标识的包；④ 任一平台构建失败时，预建的 Release 记录会以**空 Release** 留在仓库里。
- **元数据第二副本漂移（09-27）**：`DouyinLiveRecorder.egg-info/PKG-INFO` 的 `Requires-Dist: h2` 仍停在旧下限 `>=4.3.0`（清单早已抬到 `>=4.4.1` 却从未传播进元数据），内嵌 README 亦缺整段 v4.3.0 更新日志；重建修正。

**✨ 新增功能 / 改进**
- **日志房间关联字段 `extra[room]`**：`src/support/logger.py` 新增 `ROOM_FIELD` / `set_room_context()` / `get_room_context()`（内部 `ContextVar` + patcher），多房间交织日志可按房间切出。
- **Web 面板 `/health` 探活端点**：`GET /health` 返回 `{"status": "ok", "version": …}`，并把内置冒烟工具 `tool/smoke_test.py` 接通为 CI 可执行的真实 HTTP 探活。
- **W6：Apple Silicon 的 ffmpeg PATH 让位策略**：darwin + arm64 且系统 PATH 上另有原生 ffmpeg 时，不再无条件前置包内那份 x86_64 构建（需经 Rosetta 转译），五条判据全部成立才让位；其余平台逐字不变。
- **运行时二进制完整性**：`tool/build_exe.py` 新增 `_PINNED_RUNTIME_SHA256` 钉定表 + `--require-pinned`，配套 `tool/check_runtime_pins.py`（结构模式进检查 / `--strict` 进发布 prepare / `--emit-env` 供 CI 注入）；运行期首装改为「官方公布哈希优先」，发布期增加 GPG 验签与 full 包产物自检。
- **构建产物体积优化（09-24）**：排除 `PIL._avif`、`pydantic.v1.mypy`（拖入整个 mypy）、uvicorn 可选实现、`i18n/*.po` 等运行期不可达模块 + zip `compresslevel=9`，lite 产物 **82.77MB → 64.88MB（−21.6%）**、zip **54.84MB → 42.19MB（−23.1%）**，并新增 `tool/report_bundle_size.py` 度量脚本。
- **新增五类检查**：`PYTHONUTF8=1` + 「告警即失败」（消灭 GBK locale 下 isort 静默跳文件且 rc=0）、装饰器契约 AST 锁、测试卫生 AST 锁（R1–R4 / R6「手工 MonkeyPatch 必须配对 undo」）、前端目录一致性锁（`data-i18n*` 键与四语内嵌目录逐一比对）、`deps-audit` job 与覆盖率无数据硬失败。
- **动态并发下限下调**：`ConcurrencyScheduler` 的 `min_capacity` 默认值 8 → 1。
- **内置 ffmpeg 的上游与满足口径（09-26 ~ 09-27）**：Linux 两架构换源 BtbN 的 GPL 完整版资产（按 `<os>-<arch>` 运行时键分列），取数端点固定为月末不可变标签；macOS 两槽走**官方 GPG 签名档**（evermeet 不公布 SHA256，验签即该槽唯一判据，带外钉完整 40 位主钥指纹）。哈希钉定与验签两条防线互不替代。
- **`release-guard`：残缺即不发布（09-27）**：构建未全绿时用 `gh release delete` 回收预建的空/残缺 Release（默认不删 tag，修好后重跑工作流即可重新发布），并留 `::warning::`；上传步骤的 `fail_on_unmatched_files` 刻意不放宽，因为那条红是产物缺失的唯一可见信号。
- **`--dual` 双产物的两道新校验（09-27）**：出包前先清 `dist/` 里上一轮残留的 `DouyinLiveRecorder-v*.zip`（复用工作区的构建机不会把旧包一起扫进附件）；出包后必须确认 lite 与 full 两个产物路径互不相同且都已真实落件，否则当场终止。
- **CI 的 typecheck job 现在装固定版本 `pytest==9.1.1`（09-26）**：此前只装 `requirements.txt + mypy`，`tests/` 里所有 `pytest.*` 都按 `Any` 检查，fixture 与 `pytest.fail()` 的 `NoReturn` 全部失效，等于半个盲区。
- **文档体积整理（09-27，零信息删除）**：`CODE_WIKI*.md` 更新日志里 73 个历史「涉及文件（按模块分类）」清单块逐字外迁到 `docs/agent-reference/changelog-file-inventories{,-en}.md`（wiki 本体 zh −12.3% / en −5.0%），同时补回上一轮批量压缩静默用 `…` 截断的 109（zh）/ 181（en）处表格原文；`README.md` / `README_EN.md` 刻意不压缩（面向使用者的发布说明不做退化）。

**⚠️ 破坏性变更 / 行为变化**
- **Windows 运行期删除蓝奏云 ffmpeg 兜底**：自动安装只剩主源一条路（实测该直链为带人机验证的 HTML 页、伴生哈希文档全 404，不可用）；自动安装失败时改为给出明确的手动安装指引。**Windows 用户若原先依赖该兜底，需按 README 的「Windows 手动安装 ffmpeg」章节自行放置二进制。**
- **Apple Silicon 不再无条件前置包内 ffmpeg 目录**：已用 Homebrew 装原生 arm64 ffmpeg 的用户，录制子进程会改用系统那份（版本 / 编译选项可能不同）。
- **动态并发下限 8 → 1**：低负载场景下的网络并发额度不再被抬到 8。
- **布尔配置写法语义变化**：此前写 `true/false`、`1/0` 等被**静默忽略**（回落到兜底值）的键，升级后按其字面生效 —— 若你的配置依赖旧的错误兜底值，升级后生效值会改变，请在升级后核对 8 项相关配置。
- **运行时依赖 20 → 23 条**：新增显式声明 `urllib3>=2.7.0`（CVE-2026-44431）、`h2>=4.4.1`（PYSEC-2026-3628）与 `socksio>=1.0.0`（httpx 的 http2 / SOCKS 运行期依赖），`starlette` 下限 `>=0.49.1` → **`>=1.3.1`**（CVE-2026-48710 与 PYSEC-2026-2280/2281/248/249），`protobuf` 保持 `<8` 上限（gencode 兼容护栏）。
- **构建期**：`build-release.yml` prepare 以 `check_runtime_pins.py --strict` 拦下未钉定的运行时二进制。
- **`tool/build_exe.py --dual --no-zip` 现在直接报错退出**：`--dual` 分支此前从不读 `no_zip`，即用户显式写的 `--no-zip` 被静默无视、照样压出两个大 zip。矛盾参数组合改为 fail-fast，不再让其中一个参数不起作用。
- **发布附件名恢复平台与变体标识，请改用新附件**：重新发布后 Windows / Linux / macOS 的附件名形如 `DouyinLiveRecorder-v4.3.0-<os>-<arch>-lite.zip` 与 `-full.zip`；此前形如 `DouyinLiveRecorder-v4.3.zip`（无平台段、无变体段）的附件属上述缺陷产物，其内容是完整版，精简版从未存在过。
- **Linux 完整版（full）体积显著增大**：换源 BtbN 后内置 ffmpeg 资产（未压缩口径）为 linux64 ≈126.6 MB / linuxarm64 ≈108.8 MB，而原 johnvansickle amd64 静态构建 ≈41.9 MB。选择 `lite` 包可保持小体积（运行期按需自动下载）。

**🛠️ 仓库维护与质量检查**
- **测试覆盖率专项（09-21）**：`src/` 覆盖率 **73.28% → 82.03%**（仅动 `tests/`，产品代码零改动），并引入分层白名单（表默认空 + 到期即失败）。
- **四语目录持续补全与核验**：594 → **780 条**，四个目录键集逐条一致、无空值；`.mo` 随变更重编译。
- **元数据同源同步**：`AGENTS.md` 依赖条数与下限口径、`CODE_WIKI*.md` 依赖表（16 → 23 条）、`DouyinLiveRecorder.egg-info` 重建、`.dockerignore` 补 `_probe_*.py`、`config/config.toml` 补 `[Cookie] ttwid`。
- **清理已删除模块残留**：`src/weverse_auth.py` / `tests/test_weverse_auth.py`（2026-09-23 删除）在目录树、依赖注释与四语目录中的 3 条孤儿译文一并清除。
- **跨文件一致性同步（09-27）**：`requirements.txt` ↔ `pyproject.toml [project.dependencies]` 23 ↔ 23 包名集合逐项相等、三组 extras 相等；`python_build = 3.14` 与 `node_version = 24` 在 `ci.yml` / `build-release.yml` 同值；14 个本地工具目录 + 6 个运行期产物目录在 `.gitignore` / `.dockerignore` / `pyproject` 各工具排除表 / `.coveragerc-concurrency` 四套清单中无缺项；本轮实际修订仅一处——`websockets>=14.0` 被截断的行内注释按同源文本补全（规格未动）。
- **`AGENTS.md` 体积整理（09-27）**：把已在 `docs/agent-reference/measured-evidence.md` 留存的读数改为指针、合并三处真重复，97,034 → 95,715 B（−1.4%）；同时以「token 保全审计」自证未丢约束（用例名 / 错误码 / 常量与环境变量名丢失数均为 0）。

**🧪 测试与验证**
- 全量 `pytest` **3240 passed / 14 skipped / 0 failed**（09-27 本机两轮实测，其中一轮带 `--cov=src`）。原先记述为「本机 Windows 恒红」的 `test_symlinked_system_hit_inside_bundled_dir_keeps_prepending` 现已改为按主机能力显式跳过（`symlink not permitted on this host`），计入上述 14 skipped，不再是失败项。
- `black --check .` **169 files unchanged**；`isort --check-only .` 无重排；无参数 `mypy` **158 files 0 issues**（追加 `mypy --platform linux` 亦 0）；`basedpyright`（standard）**0 errors / 0 warnings / 0 notes**。
- 覆盖率：`src/` 总覆盖率 **83.91%**，`tool/check_coverage.py` **42 个模块全部达标**（`src/proto/douyin_pb2.py` 为 protoc 生成物，显式豁免）。
- `node --test tests/frontend/*.mjs` 通过；`tool/check_version.py` 与 `tool/check_runtime_pins.py` 均 rc=0；`tool/compile_po.py --check` 报 `zh_CN.mo` 与 `.po` 同步（781 条）。

## 更早的更新日志（已归档）

v4.0.0 ~ v4.2.0 的 19 条发布说明见[历史更新日志](release-notes-history-zh.md)。
