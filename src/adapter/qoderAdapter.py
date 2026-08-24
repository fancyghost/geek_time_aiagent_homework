"""Qoder 纯问答适配器：基于 qoder-agent-sdk，仅使用其 LLM 能力。

通过 tools=[]（不提供任何工具）+ max_turns=1（单轮即止）
将 Agent 运行时约束为纯问答，不涉及文件、命令等 Agent 能力。
"""
import asyncio
import concurrent.futures
import json
import queue
import threading
from collections.abc import Iterator

from qoder_agent_sdk import (
    AssistantMessage,
    PermissionMode,
    QoderAgentOptions,
    ResultMessage,
    TextBlock,
    access_token,
    query,
)

from src.adapter.modelAdapter import (
    ModelAdapter,
    ModelCapabilities,
    ModelRequest,
    ModelResult,
    StreamChunk,
)
from src.config import get_settings


class QoderChatAdapter(ModelAdapter):
    """Qoder 纯问答适配器，支持文本与提示词约束的结构化输出。"""
    name = "qoder-chat"  # 适配器名称，供上层按名选择适配器
    capabilities = ModelCapabilities(
        chat_completions=True,       # 通过 Qoder query() 接口提供统一调用
        responses=False,             # 不支持 responses 接口
        structured_output="prompt_only",  # SDK 不支持原生 schema，仅靠提示词约束
        tool_calling=False,          # 纯问答模式：不提供任何工具
        supports_temperature=False,  # Qoder SDK 无 temperature 选项
        supports_top_p=False,        # Qoder SDK 无 top_p 选项
    )

    def __init__(
        self,
        model: str | None = None,
        permission_mode: PermissionMode | None = None,
    ) -> None:
        super().__init__()
        # 优先用显式传入的参数，否则回退到 config（.env / 环境变量）中的配置
        settings = get_settings()
        self.model = model or settings.qoder_model or None
        # 纯问答模式下无工具可审批，permission_mode 仅作双保险（默认 plan）
        self.permission_mode = (
            permission_mode or settings.qoder_permission_mode or "plan"
        )
        # token 为空时不设置 auth，SDK 默认使用 qodercli 本机登录态
        self.auth = access_token(settings.qoder_access_token) if settings.qoder_access_token else None

    def generate(self, request: ModelRequest) -> ModelResult:
        """同步入口：桥接异步的 generate_async。

        同步环境直接调用；若已处于事件循环中（如 FastAPI），
        则在工作线程中运行，避免嵌套事件循环。异步上下文建议直接用 generate_async。
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # 当前无事件循环，直接运行
            return asyncio.run(self.generate_async(request))
        # 已在事件循环中：借助工作线程运行新的事件循环
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, self.generate_async(request)).result()

    async def generate_async(self, request: ModelRequest) -> ModelResult:
        """执行一次纯问答调用，返回统一格式的 ModelResult。"""
        # 校验调用接口：本适配器仅支持 chat completions（query 接口），显式指定 responses 会报错
        self.resolve_api_mode(request)
        self._maybe_inject_fault(0)  # 故障注入点（重试验证用）
        prompt = request.user

        # 结构化输出：Qoder SDK 不支持原生 schema，将 schema 注入提示词（同 dsAdapter 思路）
        if request.output_schema:
            prompt += (
                "\n最终必须只输出一个 json，并符合此 JSON Schema：\n"
                + json.dumps(request.output_schema, ensure_ascii=False)
            )

        options = QoderAgentOptions(
            system_prompt=request.system,
            model=self.model,
            tools=[],                     # 不提供任何工具，关闭 Agent 能力
            max_turns=1,                  # 单轮即止，不进入多轮 Agent 循环
            permission_mode=self.permission_mode,
            auth=self.auth,
        )
        # 注：request.max_output_tokens / temperature / top_p / tools
        # 在 Qoder SDK 中无对应选项，此处忽略

        text_parts: list[str] = []
        final: ResultMessage | None = None

        # 消费消息流：累积助手文本，捕获最终 result 消息
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
            elif isinstance(message, ResultMessage):
                final = message

        if final is None:
            raise RuntimeError("Qoder SDK 未返回 ResultMessage，调用可能异常中断")
        if final.is_error:
            errors = "; ".join(final.errors or [])
            raise RuntimeError(f"Qoder 调用执行失败：{errors}")

        # 优先使用 SDK 汇总的最终结果，其次回退到助手消息文本拼接
        text = final.result or "".join(text_parts)

        # 结构化输出时将文本解析为 dict，解析失败会抛出 JSONDecodeError
        data = json.loads(text) if request.output_schema else None

        # 用量统计：注：Qoder 运行时对部分模型（如 deepseek-v4-flash）
        # 返回的 token 数可能恒为 0，其计费口径为 credits 而非 token
        usage = final.usage or {}
        return ModelResult(
            kind="structured" if data is not None else "text",
            text=text,
            data=data,
            finish_reason=final.stop_reason or final.subtype,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            request_id=final.session_id,
        )

    def generate_stream(self, request: ModelRequest) -> Iterator[StreamChunk]:
        """流式输出：工作线程运行异步消息流，队列桥接为同步迭代器。

        注：故障注入由网关层在重试包装中按 attempt 触发，此处不重复注入。
        """
        self.resolve_api_mode(request)

        events: "queue.Queue[StreamChunk | Exception | None]" = queue.Queue()

        async def _produce() -> None:
            """消费 SDK 消息流：文本块逐块推送，ResultMessage 转为收尾块。"""
            try:
                prompt = request.user
                if request.output_schema:
                    prompt += (
                        "\n最终必须只输出一个 json，并符合此 JSON Schema：\n"
                        + json.dumps(request.output_schema, ensure_ascii=False)
                    )
                options = QoderAgentOptions(
                    system_prompt=request.system,
                    model=self.model,
                    tools=[],
                    max_turns=1,
                    permission_mode=self.permission_mode,
                    auth=self.auth,
                )
                final: ResultMessage | None = None
                async for message in query(prompt=prompt, options=options):
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock) and block.text:
                                events.put(StreamChunk(text_delta=block.text))
                    elif isinstance(message, ResultMessage):
                        final = message
                if final is None:
                    raise RuntimeError("Qoder SDK 未返回 ResultMessage，调用可能异常中断")
                if final.is_error:
                    errors = "; ".join(final.errors or [])
                    raise RuntimeError(f"Qoder 调用执行失败：{errors}")
                usage = final.usage or {}
                events.put(
                    StreamChunk(
                        finish_reason=final.stop_reason or final.subtype,
                        usage=ModelResult(
                            kind="text",
                            finish_reason=final.stop_reason or final.subtype,
                            input_tokens=usage.get("input_tokens", 0),
                            output_tokens=usage.get("output_tokens", 0),
                            request_id=final.session_id,
                        ),
                    )
                )
            except Exception as exc:  # noqa: BLE001 —— 异常经队列传回主线程抛出
                events.put(exc)
            finally:
                events.put(None)  # 哨兵：流结束

        worker = threading.Thread(
            target=lambda: asyncio.run(_produce()), daemon=True
        )
        worker.start()
        while True:
            item = events.get()
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            yield item
        worker.join(timeout=5.0)
