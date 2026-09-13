"""工具运行时：注册工具定义、按上下文计算可见工具、执行工具调用并写 Trace。"""
import json
from typing import Any, Awaitable, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from execution_context import ExecutionContext
    from tool_messages import ToolCall

# Trace 写入器：接收运行时事件（如工具执行记录）的异步回调
TraceWriter = Callable[[dict[str, Any]], Awaitable[None]]


class ToolRuntime:
    """最小工具运行时：工具以 dict 形式注册（见 search_orders_tool.SEARCH_ORDERS）。"""

    def __init__(self, trace_writer: TraceWriter) -> None:
        self._tools: dict[str, dict[str, Any]] = {}
        self._trace_writer = trace_writer

    def register(self, spec: dict[str, Any]) -> None:
        """注册一个工具定义（name / description / parameters / handler）。"""
        self._tools[spec["name"]] = spec

    def model_tools(self, ctx: "ExecutionContext") -> list[dict[str, Any]]:
        """计算当前上下文可见的工具，转为 OpenAI function-calling 格式。

        演示实现返回全部已注册工具；实际项目可在此按权限/租户过滤。
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": spec["name"],
                    "description": spec["description"],
                    "parameters": spec["parameters"],
                },
            }
            for spec in self._tools.values()
        ]

    async def execute(self, call: "ToolCall", ctx: "ExecutionContext") -> Any:
        """执行一次工具调用：解析参数 -> 调用 handler -> 写 Trace -> 回填 call_id。"""
        from tool_messages import ToolResult

        spec = self._tools.get(call.name)
        if spec is None:
            result = ToolResult(
                call_id=call.id, name=call.name,
                content=f"未知工具：{call.name}", ok=False,
            )
        else:
            try:
                arguments = json.loads(call.arguments_json or "{}")
            except json.JSONDecodeError as exc:
                result = ToolResult(
                    call_id=call.id, name=call.name,
                    content=f"参数不是合法 JSON：{exc}", ok=False,
                )
            else:
                result = spec["handler"](arguments, ctx)
                result.call_id = call.id

        # Runtime Trace：记录每次工具执行的入参与结果，便于排查
        await self._trace_writer(
            {
                "type": "tool_execute",
                "trace_id": ctx.trace_id,
                "tool": call.name,
                "arguments": call.arguments_json,
                "ok": result.ok,
                "content": result.content,
            }
        )
        return result
