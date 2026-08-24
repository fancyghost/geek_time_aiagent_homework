"""llama.cpp 本地模型适配器：基于 OpenAI 兼容接口（localhost:8080/v1）实现统一调用。

llama.cpp 的 server 原生提供 OpenAI 兼容的 /v1/chat/completions 端点，
本地部署无需 API Key（OpenAI SDK 要求 api_key 非空，传占位符即可）。
支持同步（generate）与流式（generate_stream）两种调用方式。
"""
import json
from collections.abc import Iterator
from typing import Any

from openai import OpenAI

from src.adapter.modelAdapter import (
    ModelAdapter,
    ModelCapabilities,
    ModelRequest,
    ModelResult,
    StreamChunk,
)
from src.config import get_settings


class LlamaCppChatAdapter(ModelAdapter):
    """llama.cpp 本地部署模型适配器，支持文本、json_mode 结构化输出与工具调用。"""
    name = "llama-cpp-local-chat"  # 适配器名称，供上层按名选择适配器
    capabilities = ModelCapabilities(
        chat_completions=True,       # 支持 chat completions 接口
        responses=False,             # 不支持 responses 接口
        structured_output="json_mode",  # 结构化输出仅支持 json_mode（schema 靠提示词约束）
        tool_calling=True,           # 支持工具调用（Qwen3 模板下 llama-server 可解析）
        supports_temperature=True,
        supports_top_p=True,
    )

    def __init__(self, base_url: str | None = None, model: str | None = None) -> None:
        super().__init__()
        # 优先用显式传入的参数，否则回退到 config（.env / 环境变量）中的配置
        settings = get_settings()
        self.model = model or settings.llama_model_name
        self.client = OpenAI(
            api_key="llama.cpp",     # 本地部署无需鉴权，SDK 要求非空故传占位符
            base_url=base_url or settings.llama_base_url,
            timeout=300.0,           # 本地推理较慢，超时放宽（秒）
            max_retries=0,           # 不做 SDK 层自动重试，重试策略由网关层统一控制
        )

    def _build_kwargs(self, request: ModelRequest, system: str) -> tuple[str, dict[str, Any]]:
        """构造系统提示词（含 schema 注入）与可选参数，同步/流式共用。"""
        kwargs: dict[str, Any] = {}

        # 结构化输出：llama.cpp 不支持原生 schema，将 schema 注入提示词并开启 json_mode
        if request.output_schema:
            system += (
                "\n必须输出 json，并符合此 JSON Schema：\n"
                + json.dumps(request.output_schema, ensure_ascii=False)
            )
            kwargs["response_format"] = {"type": "json_object"}

        # 可选采样参数，未传则使用模型默认值
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.top_p is not None:
            kwargs["top_p"] = request.top_p

        # 工具调用：将工具定义传给模型
        if request.tools:
            kwargs["tools"] = request.tools
        return system, kwargs

    def generate(self, request: ModelRequest) -> ModelResult:
        """执行一次 llama.cpp 本地模型调用，返回统一格式的 ModelResult。"""
        # 校验调用接口：本适配器仅支持 chat completions，显式指定 responses 会报错
        self.resolve_api_mode(request)
        self._maybe_inject_fault(0)  # 故障注入点（重试验证用）
        system, kwargs = self._build_kwargs(request, request.system)

        # 调用 chat completions 接口
        raw = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": request.user},
            ],
            max_tokens=request.max_output_tokens,
            **kwargs,
        )

        # 解析响应：取首个 choice 的文本、工具调用与用量信息
        choice = raw.choices[0]
        message = choice.message
        text = message.content or ""

        # 模型发起工具调用时转为 dict 列表，否则为空
        tool_calls = (
            [tc.model_dump() for tc in message.tool_calls]
            if message.tool_calls
            else []
        )

        return self._build_result(request, text, tool_calls, choice.finish_reason, raw.usage, raw.id)

    def generate_stream(self, request: ModelRequest) -> Iterator[StreamChunk]:
        """流式执行 llama.cpp 调用，逐块 yield StreamChunk。

        注：故障注入由网关层在重试包装中按 attempt 触发，此处不重复注入。
        """
        self.resolve_api_mode(request)
        system, kwargs = self._build_kwargs(request, request.system)

        stream = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": request.user},
            ],
            max_tokens=request.max_output_tokens,
            stream=True,
            stream_options={"include_usage": True},  # 末块携带 usage（llama-server 支持）
            **kwargs,
        )

        finish_reason: str | None = None
        usage = None
        tool_calls: list[dict[str, Any]] = []
        for chunk in stream:
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if getattr(delta, "tool_calls", None):
                tool_calls.extend(tc.model_dump() for tc in delta.tool_calls)
            if chunk.choices[0].finish_reason:
                finish_reason = chunk.choices[0].finish_reason
            if delta.content:
                yield StreamChunk(text_delta=delta.content)

        # 收尾块：结束原因 + 工具调用 + 用量统计
        yield StreamChunk(
            finish_reason=finish_reason,
            tool_calls=tool_calls or None,
            usage=self._build_result(
                request, "", tool_calls, finish_reason, usage, None
            ),
        )

    def _build_result(
        self,
        request: ModelRequest,
        text: str,
        tool_calls: list[dict[str, Any]],
        finish_reason: str | None,
        usage: Any,
        request_id: str | None,
    ) -> ModelResult:
        """同步/流式共用的 ModelResult 构造：JSON 解析与结果类型判定。"""
        # 仅在纯文本回复且要求结构化输出时解析 JSON，解析失败会抛出 JSONDecodeError
        data = (
            json.loads(text)
            if request.output_schema and not tool_calls and text
            else None
        )

        # 结果类型优先级：工具调用 > 结构化输出 > 纯文本
        if tool_calls:
            kind = "tool_calls"
        elif data is not None:
            kind = "structured"
        else:
            kind = "text"

        # 用量分类统计：llama-server 一般不提供 cached/reasoning 明细，缺失时为 0
        cached = 0
        reasoning = 0
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            cached = getattr(details, "cached_tokens", 0) or 0
        details = getattr(usage, "completion_tokens_details", None)
        if details is not None:
            reasoning = getattr(details, "reasoning_tokens", 0) or 0

        return ModelResult(
            kind=kind,
            text=text,
            data=data,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            input_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
            cached_tokens=cached,
            reasoning_tokens=reasoning,
            request_id=request_id,
        )
