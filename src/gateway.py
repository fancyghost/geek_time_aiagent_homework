"""模型网关编排层：统一调用入口。

职责（按请求处理顺序）：
1. 模板解析：版本引用（name@v1）+ {{var}} 变量替换（受控模板库）
2. 限流检查：按模型独立令牌桶，超限抛 LOCAL_RATE_LIMITED(429)
3. 模型路由：按请求 model 字段查注册表，动态选择适配器
4. 韧性调用：统一错误码 + 指数退避重试（最多 3 次，仅可重试错误）
5. 观测记录：Token 消耗（含分类）与延迟（含首 Token 延迟）落 SQLite
"""
import json
import logging
import time
import uuid
from collections.abc import Iterator
from typing import Any

from src.adapter.dsAdapter import DeepSeekChatAdapter
from src.adapter.llamaAdapter import LlamaCppChatAdapter
from src.adapter.modelAdapter import (
    ModelAdapter,
    ModelRequest,
    ModelResult,
    set_system_resolver,
)
from src.errors import ErrorCode, GatewayError, translate_exception
from src.observability.store import CallRecord, get_store
from src.prompts.registry import render_template
from src.resilience.ratelimit import check_rate_limit
from src.resilience.retry import with_retry

logger = logging.getLogger("gateway")

# ----------------------------------------------------------------------
# 模型注册表：请求中的 model 字段 → 适配器实例（屏蔽鉴权/协议差异）
# ----------------------------------------------------------------------
_adapters: dict[str, ModelAdapter] | None = None


def get_adapters() -> dict[str, ModelAdapter]:
    """模型注册表（懒加载单例）：适配器实例全局复用。"""
    global _adapters
    if _adapters is None:
        _adapters = {
            "llama-local": LlamaCppChatAdapter(),
            "deepseek-v4-flash": DeepSeekChatAdapter(),
            "deepseek-v4-pro": DeepSeekChatAdapter(model_name="deepseek-v4-pro"),
        }
    return _adapters


def get_adapter(model: str) -> ModelAdapter:
    """按 model 字段路由到适配器；未注册抛 MODEL_NOT_FOUND(404)。"""
    adapter = get_adapters().get(model)
    if adapter is None:
        raise GatewayError(
            ErrorCode.MODEL_NOT_FOUND,
            f"未注册的模型：{model!r}",
            f"可用模型：{sorted(get_adapters())}",
        )
    return adapter


# ----------------------------------------------------------------------
# 系统提示词解析：装配"版本化模板 + 变量渲染"到适配层
# ----------------------------------------------------------------------
def _default_template_resolver(name: str) -> str:
    """适配层按模板名构造 ModelRequest 时的解析器（取 latest，无变量）。"""
    return render_template(name).content


set_system_resolver(_default_template_resolver)


def _build_model_request(req: Any, system_text: str) -> ModelRequest:
    """由网关请求构造 ModelRequest：系统提示词为受控渲染结果（system_text 注入）。"""
    return ModelRequest(
        user=req.input,
        prompt_template=None,
        system_text=system_text,
        max_output_tokens=req.max_output_tokens,
        temperature=req.temperature,
        top_p=req.top_p,
        output_schema=req.output_schema,
        tools=req.tools,
        api_mode=req.api_mode,
    )


def _usage_dict(result: ModelResult) -> dict[str, int]:
    return {
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cached_tokens": result.cached_tokens,
        "reasoning_tokens": result.reasoning_tokens,
    }


# ----------------------------------------------------------------------
# 非流式调用
# ----------------------------------------------------------------------
def invoke(req: Any) -> dict[str, Any]:
    """非流式统一调用入口，返回对外响应体。"""
    request_id = uuid.uuid4().hex
    started = time.perf_counter()

    # 1. 模板解析（版本引用 + 变量替换）；未传 template 时取默认模板
    template_ref = req.template or "general_chat"
    rendered = render_template(template_ref, req.variables)
    model_request = _build_model_request(req, rendered.content)

    # 2. 限流检查（超限 429，不打上游）
    check_rate_limit(req.model)

    # 3. 模型路由
    adapter = get_adapter(req.model)

    # 4. 重试包装调用（指数退避，仅可重试错误）
    retries = 0

    def on_retry(attempt: int, err: GatewayError, backoff: float) -> None:
        nonlocal retries
        retries = attempt + 1
        logger.warning(
            "[retry] request_id=%s model=%s attempt=%s 退避 %.1fs：%s",
            request_id, req.model, attempt + 1, backoff, err.code.value,
        )

    try:
        result = with_retry(
            lambda attempt: adapter.generate(model_request),
            on_retry=on_retry,
        )
    except Exception as exc:  # noqa: BLE001 —— 失败也落观测记录后再抛出
        err = translate_exception(exc)
        get_store().record(
            CallRecord(
                request_id=request_id,
                model=req.model,
                template_ref=f"{rendered.name}@v{rendered.version}",
                status="error",
                error_code=err.code.value,
                retries=retries,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        )
        raise err from exc

    latency_ms = (time.perf_counter() - started) * 1000

    # 5. 观测落盘
    get_store().record(
        CallRecord(
            request_id=request_id,
            model=req.model,
            template_ref=f"{rendered.name}@v{rendered.version}",
            status="ok",
            retries=retries,
            latency_ms=latency_ms,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cached_tokens=result.cached_tokens,
            reasoning_tokens=result.reasoning_tokens,
        )
    )

    return {
        "request_id": request_id,
        "model": req.model,
        "template": f"{rendered.name}@v{rendered.version}",
        "kind": result.kind,
        "text": result.text,
        "data": result.data,
        "tool_calls": result.tool_calls or None,
        "finish_reason": result.finish_reason,
        "usage": _usage_dict(result),
        "latency_ms": round(latency_ms, 2),
        "retries": retries,
    }


# ----------------------------------------------------------------------
# 流式调用（SSE 事件生成器）
# ----------------------------------------------------------------------
def invoke_stream(req: Any) -> Iterator[str]:
    """流式统一调用入口：yield SSE 事件字符串。

    事件格式：
        data: {"type": "delta", "text": "..."}      文本增量
        data: {"type": "done", ...}                 收尾（用量/延迟/TTFT）
        data: {"type": "error", ...}                已开始下发后发生错误
    """
    request_id = uuid.uuid4().hex
    started = time.perf_counter()

    # 模板解析 / 限流 / 路由：在首个 SSE 事件下发前完成，错误走普通错误响应
    template_ref = req.template or "general_chat"
    rendered = render_template(template_ref, req.variables)
    model_request = _build_model_request(req, rendered.content)
    check_rate_limit(req.model)
    adapter = get_adapter(req.model)

    retries = 0
    state = {"emitted": False, "ttft_ms": None}

    def on_retry(attempt: int, err: GatewayError, backoff: float) -> None:
        nonlocal retries
        retries = attempt + 1
        logger.warning(
            "[retry] request_id=%s model=%s attempt=%s 退避 %.1fs：%s",
            request_id, req.model, attempt + 1, backoff, err.code.value,
        )

    def _call(attempt: int) -> Iterator:
        adapter._maybe_inject_fault(attempt)  # 故障注入：重试验证用
        return adapter.generate_stream(model_request)

    def _sse(payload: dict) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    chunks_iter = None
    try:
        # 重试仅发生在首个块下发前（can_retry 检查 emitted 状态）
        chunks_iter = with_retry(
            _call,
            can_retry=lambda: not state["emitted"],
            on_retry=on_retry,
        )

        text_parts: list[str] = []
        finish_reason: str | None = None
        usage_result: ModelResult | None = None
        tool_calls: list[dict[str, Any]] = []

        for chunk in chunks_iter:
            if chunk.text_delta:
                if not state["emitted"]:
                    # 首 Token 延迟：从请求开始到第一个文本块
                    state["ttft_ms"] = (time.perf_counter() - started) * 1000
                state["emitted"] = True
                text_parts.append(chunk.text_delta)
                yield _sse({"type": "delta", "text": chunk.text_delta})
            if chunk.finish_reason:
                finish_reason = chunk.finish_reason
            if chunk.usage is not None:
                usage_result = chunk.usage
            if chunk.tool_calls:
                tool_calls.extend(chunk.tool_calls)

        latency_ms = (time.perf_counter() - started) * 1000
        usage = usage_result or ModelResult(kind="text")

        # 观测落盘：流式记录 TTFT
        get_store().record(
            CallRecord(
                request_id=request_id,
                model=req.model,
                template_ref=f"{rendered.name}@v{rendered.version}",
                stream=True,
                status="ok",
                retries=retries,
                latency_ms=latency_ms,
                ttft_ms=state["ttft_ms"],
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_tokens=usage.cached_tokens,
                reasoning_tokens=usage.reasoning_tokens,
            )
        )

        # 结构化输出：流结束后整体解析 JSON（与非流式口径一致）
        full_text = "".join(text_parts)
        data = None
        kind = "text"
        if tool_calls:
            kind = "tool_calls"
        elif req.output_schema and full_text:
            data = json.loads(full_text)
            kind = "structured"

        yield _sse(
            {
                "type": "done",
                "request_id": request_id,
                "model": req.model,
                "template": f"{rendered.name}@v{rendered.version}",
                "kind": kind,
                "data": data,
                "tool_calls": tool_calls or None,
                "finish_reason": finish_reason,
                "usage": _usage_dict(usage),
                "latency_ms": round(latency_ms, 2),
                "ttft_ms": round(state["ttft_ms"], 2) if state["ttft_ms"] else None,
                "retries": retries,
            }
        )
    except Exception as exc:  # noqa: BLE001 —— 已开始下发则 SSE 内报错并落记录
        err = translate_exception(exc)
        latency_ms = (time.perf_counter() - started) * 1000
        get_store().record(
            CallRecord(
                request_id=request_id,
                model=req.model,
                template_ref=f"{rendered.name}@v{rendered.version}",
                stream=True,
                status="error",
                error_code=err.code.value,
                retries=retries,
                latency_ms=latency_ms,
                ttft_ms=state["ttft_ms"],
            )
        )
        if state["emitted"]:
            yield _sse({"type": "error", **err.to_body()["error"]})
        else:
            raise err from exc
