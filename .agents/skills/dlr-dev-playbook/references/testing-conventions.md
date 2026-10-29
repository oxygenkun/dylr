# 测试操作配方

约束见 `AGENTS.md`「测试」「测试编写强制约定」。先读被测实现和 `tests/conftest.py`，再选择桩点；优先扩充对应测试文件。

## stdlib 隔离与只读故障

`src.config.files.update_config` 和 config_io 写回均为原子替换；chmod 不能稳定制造只读错误。只替换被测模块的 os 引用，并把故障限制在本用例目标：

```python
import os
import types

real_replace = os.replace
shim = types.SimpleNamespace(**vars(os))

def deny_target(src, dst, *args, **kwargs):
    if os.fspath(dst) == os.fspath(target_path):
        raise PermissionError("read-only")
    return real_replace(src, dst, *args, **kwargs)

shim.replace = deny_target
monkeypatch.setattr(config_io, "os", shim)
```

先确认真正执行 replace 的模块；若写入已委托公共 helper，桩落在 helper 所属模块。环境变量直接用 monkeypatch.setenv/delenv。

## main 的 Popen 替身

读调用方真正使用的 poll / wait / context manager 等接口，再补必要方法；不要把整份 stdlib Popen 换掉。

```python
shim = types.SimpleNamespace(**vars(subprocess))

class FakePopen:
    def __class_getitem__(cls, item):
        return cls

    # 按被测路径提供构造和进程行为。

shim.Popen = FakePopen
monkeypatch.setattr(main, "subprocess", shim)
```

同样的 shim 方法适用于 os/time；共享模块的直接属性 patch 会影响测试外后台线程。

## 双模 collector

执行体放在 `if __name__ == "__main__": main()` 下；import 不连网、不 sleep、不清理、不退出。数字参数兼容 pytest 的多文件参数：

```python
_SECONDS_RAW = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("-") else ""
SECONDS = int(_SECONDS_RAW) if _SECONDS_RAW.isdigit() else 30
```

提交前用项目解释器对脚本跑 `--collect-only -q`，确认快速返回；新增测试优先离线可收集。

## 验证选择

- 并发：超时 join 后断言线程已退出，平台分支按当前实现分别驱动。
- 子进程：本地化系统工具按字节比较，相关文件另跑单文件模式检查码页问题。
- 安全不变量：用错误输入驱动真实生产路径；临时改动在 finally 中还原。

仅在需要 commit 时运行 pytest 与覆盖率检查；日常可编写测试，不自动执行。
