"""transfer 工具的 5 个治理测试。

覆盖点：
    1. Schema 校验（非法账号 / 多传字段）    -> INVALID_ARGUMENT
    2. 余额预检（余额不足）                  -> INSUFFICIENT_BALANCE
    3. 限额预检（超单笔限额）                -> EXCEED_LIMIT
    4. 审批参数绑定（金额被偷改）            -> APPROVAL_REQUIRED
    5. 非幂等写超时（大额触发 sleep）        -> TIMEOUT_UNKNOWN 且不重试

项目未安装 pytest-asyncio，故测试函数为同步函数，内部用 asyncio.run 驱动异步场景
（与 tests/adapter_test.py 的同步约定保持一致）。

运行（项目根目录）：
    python -m pytest tests/test_tool_governance.py -v -k "transfer"
    python tests/test_tool_governance.py
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

# 被测模块 tool_governance.py 位于 homework_week2/，而本测试在 tests/；
# 把 homework_week2 目录加入 sys.path，保证 pytest 收集与直接 python 运行都能导入。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "homework_week2"))

import tool_governance as tg


def _transfer_context(**overrides: Any) -> tg.ExecutionContext:
    """构造只放开 transfer 工具与 transfer:execute 权限的执行上下文（默认 tenant_a）。"""
    return tg.base_context(
        permissions=frozenset({"transfer:execute"}),
        allowed_tools=frozenset({"transfer"}),
        **overrides,
    )


def test_transfer_schema_rejects_extra() -> None:
    """1. 账号格式非法，或多传 approved 字段 -> INVALID_ARGUMENT，且不产生副作用。"""

    async def scenario() -> tuple[tg.ToolResult, tg.ToolResult]:
        tg.reset_side_effects()
        runtime, _, _ = tg.build_runtime()
        context = _transfer_context()
        # 账号不符合 ^ACC-[A-Z]-[0-9]{6}$
        bad_account = await runtime.invoke(
            tg.ToolCall(
                "call_bad_account",
                "transfer",
                {"from_account": "bad-account", "to_account": "ACC-A-654321", "amount": 100.0},
            ),
            context,
        )
        # approved 未在 Schema 声明，extra="forbid" 应拒绝（防止模型自造授权字段）
        extra_field = await runtime.invoke(
            tg.ToolCall(
                "call_extra_field",
                "transfer",
                {
                    "from_account": "ACC-A-123456",
                    "to_account": "ACC-A-654321",
                    "amount": 100.0,
                    "approved": True,
                },
            ),
            context,
        )
        return bad_account, extra_field

    bad_account, extra_field = asyncio.run(scenario())
    assert bad_account.ok is False
    assert bad_account.code == "INVALID_ARGUMENT"
    assert extra_field.ok is False
    assert extra_field.code == "INVALID_ARGUMENT"
    # Schema 阶段就被拦下，handler 从未执行
    assert tg.SIDE_EFFECTS["transfer_executions"] == 0


def test_transfer_precheck_insufficient() -> None:
    """2. 余额 5000 转账 6000 -> INSUFFICIENT_BALANCE，且不产生副作用。"""

    async def scenario() -> tg.ToolResult:
        tg.reset_side_effects()
        runtime, _, _ = tg.build_runtime()
        context = _transfer_context()
        # ACC-A-654321 余额 5000.0，转 6000 超出余额
        return await runtime.invoke(
            tg.ToolCall(
                "call_insufficient",
                "transfer",
                {"from_account": "ACC-A-654321", "to_account": "ACC-A-888888", "amount": 6000.0},
            ),
            context,
        )

    result = asyncio.run(scenario())
    assert result.ok is False
    assert result.code == "INSUFFICIENT_BALANCE"
    assert tg.SIDE_EFFECTS["transfer_executions"] == 0


def test_transfer_precheck_exceed_limit() -> None:
    """3. 转账 60000（超单笔限额 5 万）-> EXCEED_LIMIT，且不产生副作用。"""

    async def scenario() -> tg.ToolResult:
        tg.reset_side_effects()
        runtime, _, _ = tg.build_runtime()
        context = _transfer_context()
        # 60000 > 50000，precheck 第一步即命中单笔限额
        return await runtime.invoke(
            tg.ToolCall(
                "call_exceed_limit",
                "transfer",
                {"from_account": "ACC-A-123456", "to_account": "ACC-A-654321", "amount": 60000.0},
            ),
            context,
        )

    result = asyncio.run(scenario())
    assert result.ok is False
    assert result.code == "EXCEED_LIMIT"
    assert tg.SIDE_EFFECTS["transfer_executions"] == 0


def test_transfer_approval_binding() -> None:
    """4. 审批金额 100、执行改成 200 -> 旧审批失效，返回 APPROVAL_REQUIRED，无副作用。"""

    async def scenario() -> tg.ToolResult:
        tg.reset_side_effects()
        runtime, approvals, _ = tg.build_runtime()
        approved_args = {
            "from_account": "ACC-A-123456",
            "to_account": "ACC-A-654321",
            "amount": 100.0,
        }
        # 审批摘要与金额 100 绑定
        approvals.approve("ap_binding", _transfer_context(), "transfer", approved_args)
        # 执行时把金额偷改成 200：摘要不匹配，一次性审批应失效
        executed_args = {**approved_args, "amount": 200.0}
        return await runtime.invoke(
            tg.ToolCall("call_approval_binding", "transfer", executed_args),
            _transfer_context(approval_id="ap_binding"),
        )

    result = asyncio.run(scenario())
    assert result.ok is False
    assert result.action is tg.DecisionAction.CONFIRM
    assert result.code == "APPROVAL_REQUIRED"
    # 审批未通过，handler 从未执行
    assert tg.SIDE_EFFECTS["transfer_executions"] == 0


def test_transfer_timeout_no_retry() -> None:
    """5. 转账 90000 触发超时 -> TIMEOUT_UNKNOWN，且绝不自动重试（transfer_executions <= 1）。"""

    async def scenario() -> tg.ToolResult:
        tg.reset_side_effects()
        approvals = tg.ApprovalStore()
        audit = tg.AuditSink()
        # transfer_precheck 会在 5 万处拦截，9 万在正常流程根本到不了 handler；
        # 这里单独构造去掉 precheck 的 transfer 工具，隔离验证运行时的
        # “非幂等写操作超时 -> TIMEOUT_UNKNOWN 且 max_retries=0 不重试”行为。
        tools = [replace(t, precheck=None) for t in tg.build_tools() if t.name == "transfer"]
        runtime = tg.ToolRuntime(tools, tg.PermissionEngine(tg.DEFAULT_RULES, approvals), audit)
        args = {"from_account": "ACC-A-123456", "to_account": "ACC-A-654321", "amount": 90000.0}
        # requires_approval=True：即便绕过 precheck，也必须先拿到参数绑定审批才能进 handler
        approvals.approve("ap_timeout", _transfer_context(), "transfer", args)
        return await runtime.invoke(
            tg.ToolCall("call_timeout", "transfer", args),
            _transfer_context(approval_id="ap_timeout"),
        )

    result = asyncio.run(scenario())
    assert result.ok is False
    # WRITE + 非幂等 -> 超时后状态未知，返回 TIMEOUT_UNKNOWN 而非 TIMEOUT
    assert result.code == "TIMEOUT_UNKNOWN"
    # max_retries=0：即使超时也绝不自动重试，最多执行一次
    assert tg.SIDE_EFFECTS["transfer_executions"] <= 1


if __name__ == "__main__":
    test_transfer_schema_rejects_extra()
    test_transfer_precheck_insufficient()
    test_transfer_precheck_exceed_limit()
    test_transfer_approval_binding()
    test_transfer_timeout_no_retry()
