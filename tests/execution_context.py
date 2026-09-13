"""执行上下文：承载一次 Agent 会话的身份、权限与领域服务依赖。"""
from dataclasses import dataclass, field


@dataclass
class ExecutionContext:
    """工具执行所需的最小上下文，由调用方（Agent Loop）构造并透传给 Runtime。"""
    user_id: str                                     # 当前用户，数据过滤按此隔离
    tenant_id: str                                   # 租户隔离标识
    permissions: frozenset[str] = frozenset()        # 权限集合（如 "order:read"）
    trace_id: str = ""                               # 链路追踪 ID，写入 Runtime Trace
    order_service: object = field(default=None)      # 订单领域服务（演示用，依赖注入）
