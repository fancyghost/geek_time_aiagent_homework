"""FastAPI 应用入口：模型网关。

启动方式（项目根目录）：
    uvicorn src.main:app --port 8000
"""
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.api.routers import router
from src.errors import GatewayError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = FastAPI(
    title="模型网关",
    description=(
        "统一抽象层：按 model 字段路由到适配器，屏蔽鉴权/协议差异；"
        "提供流式 SSE、结构化输出、模板版本管理、可观测性与韧性基础。"
    ),
    version="1.0.0",
)

app.include_router(router)


@app.exception_handler(GatewayError)
async def gateway_error_handler(request: Request, exc: GatewayError) -> JSONResponse:
    """统一错误码 → 标准错误响应（{"error": {code, message, detail}}）。"""
    return JSONResponse(status_code=exc.http_status, content=exc.to_body())


@app.get("/health")
def health() -> dict:
    """健康检查。"""
    return {"status": "ok"}
