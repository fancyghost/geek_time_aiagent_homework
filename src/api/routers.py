"""FastAPI 路由：统一调用入口 + 观测/模板/模型查询 + 故障注入（验证用）。"""
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src import gateway
from src.api.schemas import InvokeRequest, InvokeResponse
from src.errors import GatewayError
from src.observability.store import get_store
from src.prompts.repository import get_repository

router = APIRouter(prefix="/v1")


# ----------------------------------------------------------------------
# 统一调用入口
# ----------------------------------------------------------------------
@router.post("/invoke", response_model=InvokeResponse)
def invoke(req: InvokeRequest) -> Any:
    """统一调用入口：按 model 路由；stream=true 时返回 SSE 流。

    同步 def：FastAPI 自动放入线程池执行，阻塞式适配器调用不阻塞事件循环。
    """
    if req.stream:
        return StreamingResponse(
            gateway.invoke_stream(req), media_type="text/event-stream"
        )
    return gateway.invoke(req)


@router.post("/invoke/stream")
def invoke_stream(req: InvokeRequest) -> StreamingResponse:
    """显式流式入口：等价于 /v1/invoke + stream=true。"""
    req.stream = True
    return StreamingResponse(
        gateway.invoke_stream(req), media_type="text/event-stream"
    )


# ----------------------------------------------------------------------
# 可观测性查询
# ----------------------------------------------------------------------
@router.get("/observability/calls")
def observability_calls(limit: int = 50) -> dict:
    """最近调用明细（Token 分类、延迟、TTFT、重试次数）。"""
    return {"calls": get_store().recent_calls(limit=limit)}


@router.get("/observability/summary")
def observability_summary() -> dict:
    """按模型聚合：调用数、Token 分类合计、平均/P95 延迟与 TTFT。"""
    return get_store().summary()


# ----------------------------------------------------------------------
# 模板与模型清单
# ----------------------------------------------------------------------
@router.get("/templates")
def list_templates() -> dict:
    """已登记模板清单（含版本与变量声明）。"""
    return {
        "templates": [
            {
                "name": info.name,
                "latest": info.latest_version,
                "versions": info.versions,
                "description": info.description,
                "variables": info.variables,
            }
            for info in get_repository().list()
        ]
    }


@router.get("/models")
def list_models() -> dict:
    """模型注册表：model 字段可用值与适配器能力。"""
    return {
        "models": [
            {
                "model": model,
                "adapter": adapter.name,
                "capabilities": {
                    "chat_completions": adapter.capabilities.chat_completions,
                    "responses": adapter.capabilities.responses,
                    "structured_output": adapter.capabilities.structured_output,
                    "tool_calling": adapter.capabilities.tool_calling,
                },
            }
            for model, adapter in sorted(gateway.get_adapters().items())
        ]
    }


# ----------------------------------------------------------------------
# 故障注入（仅验证重试用，生产环境不应暴露）
# ----------------------------------------------------------------------
class FaultConfig(BaseModel):
    """故障注入配置：让指定模型的下 N 次上游尝试抛出可重试错误。"""
    model: str
    fail_times: int


@router.post("/_debug/fault")
def inject_fault(config: FaultConfig) -> dict:
    """注入瞬态故障：随后该模型的调用前 fail_times 次尝试会失败，触发退避重试。"""
    adapter = gateway.get_adapter(config.model)
    remaining = {"n": config.fail_times}

    def hook(attempt: int) -> None:
        if remaining["n"] > 0:
            remaining["n"] -= 1
            raise ConnectionError(f"[fault-injection] 模拟上游瞬态故障（attempt={attempt}）")

    adapter.set_fault_hook(hook)
    return {"ok": True, "model": config.model, "fail_times": config.fail_times}


@router.delete("/_debug/fault")
def clear_fault(model: str) -> dict:
    """清除指定模型的故障注入。"""
    try:
        adapter = gateway.get_adapter(model)
    except GatewayError:
        adapter = None
    if adapter is not None:
        adapter.set_fault_hook(None)
    return {"ok": True, "model": model}
