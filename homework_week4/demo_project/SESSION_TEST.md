# 会话测试说明（SESSION_TEST）

> 本文件是 demo_project 的「测试说明」，与记忆演示配套。
> pi 在写入长期记忆时，会同时读取本说明与被测文件，把「测试路径 + 运行命令 + 来源」一并记下。

## 测试目标

验证用户会话（session）的核心生命周期：**创建 → 校验 → 过期 → 登出**。

## 测试入口

| 项 | 值 |
|---|---|
| 测试文件 | `tests/test_session.py` |
| 运行命令 | `python -m pytest demo_project/tests/test_session.py -q` |
| 用例数 | 4 |

## 用例清单

1. `test_create_session_is_valid` —— 新建会话后应处于有效状态。
2. `test_session_expires_after_ttl` —— 超过 TTL 后会话应失效（用固定 `now` 保证确定性，不依赖真实 sleep）。
3. `test_logout_invalidates_session` —— 登出后同一 token 应立即失效。
4. `test_unknown_token_is_invalid` —— 未知 token 一律判为无效。

## 备注

- 被测的 `SessionStore` 为自包含的极简实现，测试可独立运行，不依赖项目其它模块。
