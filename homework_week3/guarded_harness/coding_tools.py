"""coding_tools.py —— 把治理层（Gate）适配成 smolagents 的工具与 Agent。

设计目标（对应验收 T1）：**不修改 smolagents 源码**，只通过其公开扩展点接入：

- ``GuardedTool``：一个 ``smolagents.Tool`` 子类。它的 ``forward`` 不做任何业务，
  只把“模型想调用什么”原样转交给 ``Gate``（唯一入口），再把结构化的 ``GateResult``
  以 JSON 字符串返回，作为 observation 喂回模型。所有工具共用这一个类，
  差异全部来自 ``ToolSpec``（参数模型、策略、handler）。
- ``GuardedToolCallingAgent``：一个 ``ToolCallingAgent`` 子类，重写 ``process_tool_calls``，
  使每次工具调用都强制经过 ``Gate``；被拒动作不会抛异常打断循环，而是把拒绝原因
  作为 observation 返回，让模型自我纠正。
- 系统注入的 ``tool_call_id`` 对模型不可见：它由框架在调用时补入，用于审计链对齐。

关键点：
1. ``skip_forward_signature_validation = True`` —— 我们的 ``forward(**kwargs)`` 使用泛化签名，
   跳过 smolagents 对 forward 形参与 inputs 一致性的校验（否则会因 tool_call_id 不匹配而报错）。
2. ``__init__`` 必须在 ``super().__init__()`` 之前设置好 name/description/inputs/output_type，
   因为 ``Tool.__init_subclass__`` 会在实例化后立刻调用 ``validate_arguments()``。
"""

from __future__ import annotations

import json
from collections.abc import Generator
from typing import Any

from smolagents.agents import ToolCallingAgent, ToolOutput
from smolagents.memory import ToolCall
from smolagents.models import ChatMessage
from smolagents.tools import Tool

from catalog import ToolSpec
from gate import Gate, GateRequest

# 系统在调用工具时注入的隐藏参数名（承载 tool_call_id，用于审计对齐）。
TOOL_CALL_ID = "tool_call_id"


class GuardedTool(Tool):
    """把一次工具调用转发给 ``Gate`` 的“哑”工具。

    ``inputs`` 直接取自 ``ToolSpec.model_inputs``（不含系统注入的 ``tool_call_id``），
    因此模型看到的 schema 与实际受控参数一致；``tool_call_id`` 由 Agent 在调用时补入。
    """

    # 使用泛化 forward 签名，跳过 smolagents 的形参一致性校验。
    skip_forward_signature_validation = True

    def __init__(self, spec: ToolSpec, gate: Gate) -> None:
        self.spec = spec
        self.gate = gate
        # 先设置校验所需属性，再调用 super().__init__()（其后会触发 validate_arguments）。
        self.name = spec.name
        self.description = spec.description
        self.inputs = {k: dict(v) for k, v in spec.model_inputs.items()}
        self.output_type = "string"
        super().__init__()

    def forward(self, **kwargs: Any) -> str:  # type: ignore[override]
        """把调用转交 Gate，返回结构化裁决的 JSON 字符串（作为 observation）。"""

        arguments = dict(kwargs)
        tool_call_id = str(arguments.pop(TOOL_CALL_ID, "") or "")
        request = GateRequest(tool_call_id=tool_call_id, tool_name=self.name, arguments=arguments)
        result = self.gate.execute(request)
        return json.dumps(result.to_dict(), ensure_ascii=False)


class GuardedToolCallingAgent(ToolCallingAgent):
    """强制“每个工具调用都经过 Gate”的 ToolCallingAgent。

    仅重写 ``process_tool_calls``：保持与父类相同的产出契约（依次 yield ``ToolCall`` 与
    ``ToolOutput``，并在末尾回填 ``memory_step``），但把实际执行替换为经 Gate 的
    ``_invoke_guarded``。被 Gate 拒绝的调用返回结构化拒绝（不抛异常），循环得以继续。
    """

    def process_tool_calls(  # type: ignore[override]
        self, chat_message: ChatMessage, memory_step
    ) -> Generator[ToolCall | ToolOutput]:
        tool_calls = list(chat_message.tool_calls or [])
        outputs: dict[str, ToolOutput] = {}
        parsed_calls: list[ToolCall] = []

        for chat_tool_call in tool_calls:
            name = chat_tool_call.function.name
            arguments = chat_tool_call.function.arguments
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except (ValueError, TypeError):
                    arguments = {"_raw": arguments}
            tool_call = ToolCall(name=name, arguments=arguments, id=chat_tool_call.id)
            parsed_calls.append(tool_call)
            yield tool_call

            observation = self._invoke_guarded(tool_call)
            tool_output = ToolOutput(
                id=tool_call.id,
                output=observation,
                is_final_answer=(name == "final_answer"),
                observation=observation,
                tool_call=tool_call,
            )
            outputs[tool_output.id] = tool_output
            yield tool_output

        # 回填记忆步骤（与父类语义一致：按 id 排序拼接 observation）。
        memory_step.tool_calls = parsed_calls
        memory_step.observations = memory_step.observations or ""
        for tool_output in outputs.values():
            memory_step.observations += tool_output.observation + "\n"
        memory_step.observations = (
            memory_step.observations.rstrip("\n") if memory_step.observations else memory_step.observations
        )

    def _invoke_guarded(self, tool_call: ToolCall) -> str:
        """经 Gate 执行一次工具调用，返回 observation 字符串。"""

        tool = self.tools.get(tool_call.name)
        if tool is None:
            # 未知工具：交给 Gate 统一裁决（也会走审计），这里直接返回结构化拒绝。
            return json.dumps(
                {
                    "ok": False,
                    "tool_name": tool_call.name,
                    "tool_call_id": tool_call.id,
                    "decision": "deny",
                    "code": "TOOL_NOT_FOUND",
                    "message": f"未知工具：{tool_call.name!r}",
                    "content": None,
                    "is_final_answer": False,
                },
                ensure_ascii=False,
            )
        arguments = dict(tool_call.arguments) if isinstance(tool_call.arguments, dict) else {}
        arguments[TOOL_CALL_ID] = tool_call.id
        return str(tool(**arguments)).strip()


def build_guarded_tools(gate: Gate, specs: dict[str, ToolSpec]) -> list[GuardedTool]:
    """按工具目录构造一组 ``GuardedTool``（共享同一个 Gate 实例）。"""

    return [GuardedTool(spec, gate) for spec in specs.values()]
