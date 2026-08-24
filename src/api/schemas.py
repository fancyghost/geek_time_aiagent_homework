"""API 请求/响应模型（Pydantic v2）。

安全约定：请求体不包含自由文本系统提示词字段；系统提示词只能经
template 字段引用受控模板库（含版本与变量），防止提示词注入。
"""
from typing import Any, Literal

from pydantic import BaseModel, Field


class InvokeRequest(BaseModel):
    """统一调用请求：model 字段决定路由到哪个适配器。"""
    model: str = Field(description="模型名（路由键），见 GET /v1/models")
    input: str = Field(description="用户输入（不可信数据，仅作为对话内容）")
    template: str | None = Field(
        default=None,
        description="模板引用：name / name@v1 / name@latest；缺省 general_chat",
    )
    variables: dict[str, str] | None = Field(
        default=None, description="模板变量（仅允许填充模板声明的占位符）"
    )
    stream: bool = Field(default=False, description="是否流式输出（SSE）")
    output_schema: dict[str, Any] | None = Field(
        default=None, description="结构化输出 JSON Schema（response_format 约束）"
    )
    tools: list[dict[str, Any]] | None = Field(
        default=None, description="工具定义（chat 嵌套格式）"
    )
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int = Field(default=1024, ge=1, le=32768)
    api_mode: Literal["chat_completions", "responses"] | None = Field(
        default=None, description="指定底层接口；缺省由适配器默认选择"
    )


class UsageOut(BaseModel):
    """Token 消耗（含分类统计）。"""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0      # 分类：缓存命中
    reasoning_tokens: int = 0   # 分类：思维链


class InvokeResponse(BaseModel):
    """非流式调用响应。"""
    request_id: str
    model: str
    template: str | None = None
    kind: str
    text: str | None = None
    data: dict[str, Any] | None = None
    tool_calls: list[dict[str, Any]] | None = None
    finish_reason: str | None = None
    usage: UsageOut
    latency_ms: float
    retries: int = 0
