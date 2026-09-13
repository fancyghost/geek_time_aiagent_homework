import asyncio
import json
import os
from datetime import date

from openai import OpenAI

from execution_context import ExecutionContext
from search_orders_tool import DemoOrderService, SEARCH_ORDERS
from tool_messages import ToolCall
from tool_runtime import ToolRuntime

MAX_STEPS = 4

# 本地 llama.cpp 服务端点与模型名，可通过环境变量覆盖（与 .env 中的配置同名）
LLAMA_BASE_URL = os.environ.get("LLAMA_BASE_URL", "http://localhost:8080/v1")
LLAMA_MODEL_NAME = os.environ.get(
    "LLAMA_MODEL_NAME", "Qwen3.8-27B-NVFP4-MTP-HIGHEST"
)


def create_client() -> OpenAI:
    # 本地部署无需鉴权，OpenAI SDK 要求 api_key 非空故传占位符；超时放宽（本地推理较慢）
    return OpenAI(
        api_key="llama.cpp",
        base_url=LLAMA_BASE_URL,
        timeout=300.0,
        max_retries=0,
    )


async def run_order_agent(
    user_text: str,
    runtime: "ToolRuntime",
    ctx: "ExecutionContext",
    client: OpenAI,
) -> str:
    # 注入当前日期，模型才能把“昨天”等相对时间换算成 YYYY-MM-DD 去调工具
    messages = [
        {
            "role": "system",
            "content": (
                "你是订单助手，不得编造订单。"
                f"今天是 {date.today().isoformat()}，"
                "遇到相对日期（如昨天）请先换算成 YYYY-MM-DD 再查询。"
            ),
        },
        {"role": "user", "content": user_text},
    ]

    for step in range(1, MAX_STEPS + 1):
        visible_tools = runtime.model_tools(ctx)
        print(f"\n=== 第 {step} 轮：计算当前可见工具 ===")
        print([tool["function"]["name"] for tool in visible_tools])
        print("=== 请求本地 llama.cpp ===")
        response = client.chat.completions.create(
            model=LLAMA_MODEL_NAME,
            messages=messages,
            tools=visible_tools,
            tool_choice="auto",
        )

        assistant = response.choices[0].message
        messages.append(assistant.model_dump(exclude_none=True))
        tool_calls = assistant.tool_calls or []

        if not tool_calls:
            print("模型未返回 Tool Call，Agent Loop 结束。")
            return assistant.content or ""

        print(f"模型返回 {len(tool_calls)} 个 Tool Call。")
        for raw_call in tool_calls:
            call = ToolCall(
                id=raw_call.id,
                name=raw_call.function.name,
                arguments_json=raw_call.function.arguments,
            )
            print(
                f"执行 Runtime: id={call.id}, name={call.name}, "
                f"arguments={call.arguments_json}"
            )
            result = await runtime.execute(call, ctx)
            messages.append(result.to_model_message())
            print(f"写回 Tool Result: {result.content}")

    raise RuntimeError("agent exceeded maximum steps")


async def trace_writer(event: dict) -> None:
    print(f"Runtime Trace: {json.dumps(event, ensure_ascii=False)}")


async def main() -> None:
    runtime = ToolRuntime(trace_writer)
    runtime.register(SEARCH_ORDERS)
    ctx = ExecutionContext(
        user_id="user_demo",
        tenant_id="tenant_demo",
        permissions=frozenset({"order:read"}),
        trace_id="trace_demo_002",
        order_service=DemoOrderService(),
    )
    answer = await run_order_agent(
        "查一下我昨天创建、还没有支付的订单。",
        runtime,
        ctx,
        create_client(),
    )
    print("\n=== 最终回答 ===")
    print(answer)


if __name__ == "__main__":
    asyncio.run(main())
