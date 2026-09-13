"""订单查询工具：演示用的领域服务、工具定义与执行入口。"""
import json
from datetime import date, timedelta
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from execution_context import ExecutionContext
    from tool_messages import ToolResult


class DemoOrderService:
    """演示订单服务：内置少量样例数据，"昨天"相对当前日期动态生成。"""

    def __init__(self) -> None:
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        today = date.today().isoformat()
        self._orders = [
            {
                "order_id": "ORD-1001",
                "user_id": "user_demo",
                "status": "unpaid",
                "amount": 129.0,
                "created_at": yesterday,
                "items": ["机械键盘"],
            },
            {
                "order_id": "ORD-1002",
                "user_id": "user_demo",
                "status": "paid",
                "amount": 45.5,
                "created_at": today,
                "items": ["数据线"],
            },
            {
                "order_id": "ORD-1003",
                "user_id": "user_other",
                "status": "unpaid",
                "amount": 899.0,
                "created_at": yesterday,
                "items": ["显示器"],
            },
        ]

    def search_orders(
        self,
        user_id: str,
        status: str | None = None,
        created_on: str | None = None,
    ) -> list[dict[str, Any]]:
        """按用户过滤订单，可选按状态与创建日期（YYYY-MM-DD）进一步筛选。"""
        result = [o for o in self._orders if o["user_id"] == user_id]
        if status:
            result = [o for o in result if o["status"] == status]
        if created_on:
            result = [o for o in result if o["created_at"] == created_on]
        return result


def _run_search_orders(
    arguments: dict[str, Any], ctx: "ExecutionContext"
) -> "ToolResult":
    """search_orders 执行入口：权限校验 + 参数解析 + 调用领域服务。"""
    from tool_messages import ToolResult

    # 权限校验：无 order:read 权限直接拒绝，模型会看到失败原因
    if "order:read" not in ctx.permissions:
        return ToolResult(
            call_id="", name="search_orders",
            content="权限不足：需要 order:read", ok=False,
        )

    status = arguments.get("status")
    created_on = arguments.get("created_on")
    # 模型可能用相对语义表达"昨天"，允许同义词
    if created_on in ("yesterday", "昨天"):
        created_on = (date.today() - timedelta(days=1)).isoformat()

    orders = ctx.order_service.search_orders(
        user_id=ctx.user_id, status=status, created_on=created_on
    )
    return ToolResult(
        call_id="", name="search_orders",
        content=json.dumps(orders, ensure_ascii=False),
    )


SEARCH_ORDERS: dict[str, Any] = {
    "name": "search_orders",
    "description": "查询当前用户的订单，可按状态和创建日期过滤。",
    "parameters": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["unpaid", "paid", "shipped", "completed", "cancelled"],
                "description": "订单状态，缺省表示不过滤",
            },
            "created_on": {
                "type": "string",
                "description": "创建日期 YYYY-MM-DD，缺省表示不过滤",
            },
        },
        "required": [],
    },
    "handler": _run_search_orders,
}
