# 解析返回契约

约束见 `AGENTS.md` 与 `docs/agent-reference/known-pitfalls.md`「装饰器与平台兜底契约」。

先看函数返回注解及全部调用方，再选择 dict 或 None 兜底；解包前检查空结果。新增解析测试同时覆盖正常、离线、畸形响应，区分未开播与接口失败。

JSON 复用 _loads_dict / _safe_loads 与 _dig 等 helper；HTML/WAF/截断响应保留现有告警和空字典契约。裸 json.loads 例外以 test_spider_hardening.py 的棘轮为准，不复制旧调用数量。

挪函数或装饰器时检查实际返回值及熔断样本变化。
