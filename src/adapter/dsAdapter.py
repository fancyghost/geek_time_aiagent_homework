"""DeepSeek 模型适配器：基于 OpenAI 兼容接口实现统一的模型调用。

同时支持两种调用接口，通过 ModelRequest.api_mode 切换（默认 chat_completions）：
- chat completions（client.chat.completions.create）：传统对话补全接口
- responses（client.responses.create）：DeepSeek 新增的 OpenAI Responses API
  兼容接口，无状态，text.format 原生支持 json_schema 结构化输出

两种接口均提供同步（generate）与流式（generate_stream，SSE 逐块）两种调用方式。
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


def _thinking_extra_body(thinking: bool) -> dict[str, dict[str, str]]:
    """构造 thinking 开关参数：默认 disabled，显式开启时为 enabled。"""
    return {"thinking": {"type": "enabled" if thinking else "disabled"}}


def _usage_details(usage: Any) -> tuple[int, int]:
    """从 usage 明细提取 (cached_tokens, reasoning_tokens)，字段缺失时为 0。"""
    cached = 0
    reasoning = 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", 0) or 0
    details = getattr(usage, "completion_tokens_details", None)
    if details is not None:
        reasoning = getattr(details, "reasoning_tokens", 0) or 0
    # responses 接口的用量明细字段名不同
    details = getattr(usage, "input_tokens_details", None)
    if details is not None:
        cached = cached or (getattr(details, "cached_tokens", 0) or 0)
    details = getattr(usage, "output_tokens_details", None)
    if details is not None:
        reasoning = reasoning or (getattr(details, "reasoning_tokens", 0) or 0)
    return cached, reasoning


class DeepSeekChatAdapter(ModelAdapter):
    """DeepSeek chat 模型适配器，支持文本、结构化输出、工具调用与双接口（chat/responses）。"""
    name = "deepseek-v4-flash-chat"  # 适配器名称，供上层按名选择适配器
    capabilities = ModelCapabilities(
        chat_completions=True,       # 支持 chat completions 接口
        responses=True,              # 支持 responses 接口（DeepSeek 官方已兼容）
        structured_output="json_mode",  # chat 接口仅 json_mode；responses 接口原生支持 json_schema（更强）
        tool_calling=True,           # 支持工具调用（两种接口均传递并解析）
        supports_temperature=True,  # 仅 non-thinking 生效
        supports_top_p=True,         # 仅 non-thinking 生效
    )

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model_name: str | None = None) -> None:
        super().__init__()
        # 优先用显式传入的参数，否则回退到 config（.env / 环境变量）中的配置
        settings = get_settings()
        # 官方模型名：同协议不同档位（如 v4-flash / v4-pro）仅需注册时传入不同 model_name
        self.model_name = model_name or settings.deepseek_model_name
        self.client = OpenAI(
            api_key=api_key or settings.deepseek_api_key,
            base_url=base_url or settings.deepseek_base_url,
            timeout=30.0,    # 单次请求超时（秒）
            max_retries=0,   # 不做 SDK 层自动重试，重试策略由网关层统一控制
        )

    # ------------------------------------------------------------------
    # 同步调用
    # ------------------------------------------------------------------
    def generate(self, request: ModelRequest) -> ModelResult:
        """执行一次 DeepSeek 调用，按 api_mode 路由到对应接口实现。"""
        self._maybe_inject_fault(0)  # 故障注入点（重试验证用）
        if self.resolve_api_mode(request) == "responses":
            return self._generate_responses(request)
        return self._generate_chat(request)

    def _chat_kwargs(self, request: ModelRequest, system: str) -> tuple[str, dict[str, Any]]:
        """构造 chat 接口的系统提示词（含 schema 注入）与可选参数。"""
        kwargs: dict[str, Any] = {}

        # 结构化输出：chat 接口不支持原生 schema，将 schema 注入提示词并开启 json_mode
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

    def _generate_chat(self, request: ModelRequest) -> ModelResult:
        """通过 chat completions 接口调用，返回统一格式的 ModelResult。"""
        system, kwargs = self._chat_kwargs(request, request.system)

        # 调用 chat completions 接口；thinking 按请求开关（默认关闭深度思考模式）
        raw = self.client.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": request.user},
            ],
            max_tokens=request.max_output_tokens,
            extra_body=_thinking_extra_body(request.thinking),
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

        usage = raw.usage
        cached, reasoning = _usage_details(usage)
        return self._build_result(
            request=request,
            text=text,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
            input_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
            cached_tokens=cached,
            reasoning_tokens=reasoning,
            request_id=raw.id,
        )

    def _responses_kwargs(self, request: ModelRequest) -> dict[str, Any]:
        """构造 responses 接口的可选参数（含 DeepSeek 特有格式转换）。"""
        kwargs: dict[str, Any] = {}

        # 结构化输出：responses 接口原生支持 json_schema（text.format 完整支持）
        # 注意与 OpenAI 差异：DeepSeek 要求 schema 直接放 format.schema 字段
        if request.output_schema:
            kwargs["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "output",
                    "schema": request.output_schema,
                }
            }

        # 可选采样参数，未传则使用模型默认值
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.top_p is not None:
            kwargs["top_p"] = request.top_p

        # 工具调用：ModelRequest.tools 为 chat 嵌套格式，需转为 responses 扁平格式
        if request.tools:
            kwargs["tools"] = [self._to_responses_tool(t) for t in request.tools]
        return kwargs

    def _generate_responses(self, request: ModelRequest) -> ModelResult:
        """通过 responses 接口调用，返回统一格式的 ModelResult。"""
        kwargs = self._responses_kwargs(request)

        # 调用 responses 接口；thinking 按请求开关（默认关闭，不支持的参数服务端会静默忽略）
        raw = self.client.responses.create(
            model=self.model_name,
            instructions=request.system,
            input=request.user,
            max_output_tokens=request.max_output_tokens,
            extra_body=_thinking_extra_body(request.thinking),
            **kwargs,
        )

        # 文本输出：SDK 汇总各 message item 的 output_text
        text = raw.output_text or ""

        # 工具调用：function_call item 归一化为 chat completions 同款结构
        tool_calls = [
            {
                "id": item.call_id,
                "type": "function",
                "function": {"name": item.name, "arguments": item.arguments},
            }
            for item in raw.output
            if item.type == "function_call"
        ]

        # responses 接口无 finish_reason，按 status 映射为 chat 语义
        finish_reason = {"completed": "stop", "incomplete": "length"}.get(
            raw.status, raw.status
        )

        usage = raw.usage
        cached, reasoning = _usage_details(usage)
        return self._build_result(
            request=request,
            text=text,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
            cached_tokens=cached,
            reasoning_tokens=reasoning,
            request_id=raw.id,
        )

    # ------------------------------------------------------------------
    # 流式调用（SSE 逐块）
    # ------------------------------------------------------------------
    def generate_stream(self, request: ModelRequest) -> Iterator[StreamChunk]:
        """流式执行 DeepSeek 调用，逐块 yield StreamChunk。

        注：故障注入由网关层在重试包装中按 attempt 触发，此处不重复注入。
        """
        if self.resolve_api_mode(request) == "responses":
            yield from self._stream_responses(request)
        else:
            yield from self._stream_chat(request)

    def _stream_chat(self, request: ModelRequest) -> Iterator[StreamChunk]:
        """chat completions 流式：逐块文本增量，末块携带用量与结束原因。"""
        system, kwargs = self._chat_kwargs(request, request.system)

        stream = self.client.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": request.user},
            ],
            max_tokens=request.max_output_tokens,
            extra_body=_thinking_extra_body(request.thinking),
            stream=True,
            stream_options={"include_usage": True},  # 末块携带 usage
            **kwargs,
        )

        finish_reason: str | None = None
        usage = None
        tool_calls: list[dict[str, Any]] = []
        for chunk in stream:
            if getattr(chunk, "usage", None):
                usage = chunk.usage  # include_usage 的末块（choices 为空）
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
        cached, reasoning = _usage_details(usage)
        yield StreamChunk(
            finish_reason=finish_reason,
            tool_calls=tool_calls or None,
            usage=self._build_result(
                request=request,
                text="",
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                input_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
                output_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
                cached_tokens=cached,
                reasoning_tokens=reasoning,
                request_id=None,
            ),
        )

    def _stream_responses(self, request: ModelRequest) -> Iterator[StreamChunk]:
        """responses 流式：消费 SSE 事件流，文本增量逐块下发。"""
        kwargs = self._responses_kwargs(request)

        stream = self.client.responses.create(
            model=self.model_name,
            instructions=request.system,
            input=request.user,
            max_output_tokens=request.max_output_tokens,
            extra_body=_thinking_extra_body(request.thinking),
            stream=True,
            **kwargs,
        )

        final = None  # response.completed / response.incomplete 事件携带完整 response
        tool_calls: list[dict[str, Any]] = []
        for event in stream:
            event_type = getattr(event, "type", "")
            if event_type == "response.output_text.delta":
                if event.delta:
                    yield StreamChunk(text_delta=event.delta)
            elif event_type == "response.output_item.done":
                item = getattr(event, "item", None)
                if item is not None and getattr(item, "type", "") == "function_call":
                    tool_calls.append(
                        {
                            "id": item.call_id,
                            "type": "function",
                            "function": {"name": item.name, "arguments": item.arguments},
                        }
                    )
            elif event_type in ("response.completed", "response.incomplete"):
                final = getattr(event, "response", None)

        # 收尾块：从最终 response 提取状态与用量
        status = getattr(final, "status", None) if final else None
        finish_reason = {"completed": "stop", "incomplete": "length"}.get(status, status)
        usage = getattr(final, "usage", None) if final else None
        cached, reasoning = _usage_details(usage)
        yield StreamChunk(
            finish_reason=finish_reason,
            tool_calls=tool_calls or None,
            usage=self._build_result(
                request=request,
                text="",
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
                output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
                cached_tokens=cached,
                reasoning_tokens=reasoning,
                request_id=getattr(final, "id", None) if final else None,
            ),
        )

    # ------------------------------------------------------------------
    # 工具与结果构造
    # ------------------------------------------------------------------
    @staticmethod
    def _to_responses_tool(tool: dict[str, Any]) -> dict[str, Any]:
        """将 chat 格式的嵌套工具定义转为 responses 接口的扁平格式。

        chat 格式：{"type": "function", "function": {name, description, parameters}}
        responses 格式：{"type": "function", name, description, parameters}
        """
        if tool.get("type") == "function" and "function" in tool:
            return {"type": "function", **tool["function"]}
        # 非 function 工具（如 web_search）本身即 responses 格式，直接透传
        return tool

    def _build_result(
        self,
        request: ModelRequest,
        text: str,
        tool_calls: list[dict[str, Any]],
        finish_reason: str | None,
        input_tokens: int,
        output_tokens: int,
        request_id: str | None,
        cached_tokens: int = 0,
        reasoning_tokens: int = 0,
    ) -> ModelResult:
        """两种接口共用的 ModelResult 构造：JSON 解析与结果类型判定。"""
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

        return ModelResult(
            kind=kind,
            text=text,
            data=data,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            reasoning_tokens=reasoning_tokens,
            request_id=request_id,
        )
