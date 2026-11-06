# 配置存储

[架构导航](README.md) · [文档导航](../README.md)

配置键、填写示例和房间列表格式统一见 [配置说明](../configuration.md) 与 [TOML 模板](../../config/config.toml.example)。本页只说明配置实现，不再保留旧 wiki 的 INI 主配置表和重复默认值。

## 主配置与迁移

主配置为 `config/config.toml`；直播间列表仍为 `config/URL_config.ini`，不是主配置迁移遗漏。

- [store.py](../../src/config/store.py) 的 `ConfigStore` 使用 tomllib / tomlkit 解析 TOML，用 pydantic `TypeAdapter` 校验已声明的布尔、整数与浮点键，保留注释和原键写法。读取接口 `get()` / `items()` 返回字符串视图，`native()` 返回标量原值；布尔字符串视图保持「是/否」。
- 整份解析事务式提交；热加载失败保留上次有效文档，单键非法值保留原文，交由读取方告警并回退默认。`reload_if_changed()` 按文件内容摘要而非 mtime 判断变化，供引擎下一轮检测加载。
- `ensure_config_migrated()` 只在 TOML 不存在时迁移旧 INI；成功转换后尝试将旧文件改名为 `config.ini.migrated`。已有 TOML 不被旧 INI 覆盖，首次启动无配置时创建 TOML 头部再由读取方补缺省值。
- [reader.py](../../src/config/reader.py) 的容错 INI 解析和重复键诊断仅服务旧配置迁移；不把「INI 同节重复键容错」当成当前 TOML 语法规则。

## 读写接口

| 接口 | 所属模块 | 行为 |
| --- | --- | --- |
| `read_ini_value()` / `read_config_value()` | [files.py](../../src/config/files.py) | 只读单个 TOML 配置项；旧函数名不代表仍读 INI |
| `update_config()` | [files.py](../../src/config/files.py) | 持共享文件锁、校验后原子写回 TOML |
| `read_config_value()` / `read_config_bool()` | [io.py](../../src/config/io.py) | 引擎读取、缺省值补写和统一布尔解析 |
| `ConfigStore.set()` / `write()` | [store.py](../../src/config/store.py) | 先修改内存文档，再原子落盘；读取失败的存储视图拒绝写回 |
| 房间编辑与配置备份 | [io.py](../../src/config/io.py) | 编辑 URL 列表、维护脱敏备份 |
| 面板配置读写 | [config.py](../../src/api/config.py) | Web 配置、敏感值遮罩、目标键和值校验 |

写入时复用共享 `state.file_update_lock`；布尔 token 的单一解析入口为 [booleans.py](../../src/config/booleans.py)。键名、原文保留和 TLS 约束见 [配置已知坑](../agent-reference/known-pitfalls.md#配置键名布尔口径与-ssl-验证)。

## 主播名自动更新

开启「是否自动更新主播名(是/否)」后，每轮解析到平台最新主播名与当前使用名不一致时，执行以下流程：

1. 将保存目录中的旧主播名文件夹改为新名；目标已存在则逐项合并。
2. 同步重命名子目录内以旧主播名为前缀的录制、弹幕 SRT、时间字幕等文件，以及以 `_{旧主播名}` 结尾的标题目录。
3. 按 URL 精确匹配房间配置行并更新名称，保留画质、注释前缀与换行风格，全角冒号统一为半角。

触发点在每轮解析之后、录制启动之前，不修改正在录制的文件。自定义录制平台的随机名称和「空白昵称」等无效名称跳过。先修改文件系统，再写房间配置，两者全部成功才切换本轮使用名；失败继续使用旧名，下轮幂等重试。

被后台转码或播放器占用的个别文件改名失败只告警跳过，其余文件继续处理，下轮补齐；同步清理旧名状态条目，避免监控页挂旧名。配置写入与 Web 编辑共用文件锁。关闭该选项则保持手填名称。

实现位于 [session.py](../../src/recording/session.py) 与 [io.py](../../src/config/io.py)，面板安全边界见 [Web 专题](web.md)。

> 返回[架构导航](README.md) · [文档导航](../README.md)。
