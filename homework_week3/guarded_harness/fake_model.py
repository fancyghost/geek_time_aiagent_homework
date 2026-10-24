"""fake_model.py —— 确定性脚本模型（离线可复现，无网络、无随机）。

用途：驱动 ``GuardedToolCallingAgent`` 的 ReAct 循环，让测试与 Demo **完全可复现**。
它按脚本顺序逐步返回一个 ``ChatMessage``（含单个工具调用）；脚本耗尽后返回一个
不含工具调用的消息，主循环据此判定“模型无更多动作”（MODEL_ERROR）。

这不是对真实 LLM 的模拟，而是一个**可控的动作序列发生器**：治理层（Gate/Sandbox/
Completion/Checkpoint）的正确性不依赖模型的智能，只依赖它“想调用什么工具、传什么参数”。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import _bootstrap  # noqa: F401  # 必须在导入 smolagents 之前，确保其源码在 sys.path 上

from smolagents.models import (
    ChatMessage,
    ChatMessageToolCall,
    ChatMessageToolCallFunction,
    MessageRole,
    Model,
)


@dataclass
class ScriptedAction:
    """脚本中的一步：调用某个工具并传入参数（``tool_call_id`` 可选，缺省自动编号）。"""

    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    tool_call_id: str | None = None

    def resolved_id(self, fallback: str) -> str:
        return self.tool_call_id or fallback

    @staticmethod
    def call(tool_name: str, tool_call_id: str | None = None, **arguments: Any) -> "ScriptedAction":
        return ScriptedAction(tool_name=tool_name, arguments=dict(arguments), tool_call_id=tool_call_id)

    @staticmethod
    def final(answer: str, tool_call_id: str | None = None) -> "ScriptedAction":
        return ScriptedAction(tool_name="final_answer", arguments={"answer": answer}, tool_call_id=tool_call_id)


class FakeModel(Model):
    """按脚本产出工具调用的假模型。"""

    def __init__(
        self,
        actions: list[ScriptedAction] | None = None,
        *,
        exhausted_content: str = "（脚本已耗尽：模型没有更多动作）",
        model_id: str = "fake-model",
    ) -> None:
        super().__init__(model_id=model_id)
        self._actions: list[ScriptedAction] = list(actions or [])
        self._index = 0
        self.exhausted_content = exhausted_content

    # ---- 脚本控制 ----
    def push(self, *actions: ScriptedAction) -> None:
        self._actions.extend(actions)

    @property
    def remaining(self) -> int:
        return max(0, len(self._actions) - self._index)

    # ---- Model 接口 ----
    def generate(  # type: ignore[override]
        self,
        messages: list[ChatMessage],
        stop_sequences: list[str] | None = None,
        response_format: dict[str, str] | None = None,
        tools_to_call_from: list[Any] | None = None,
        **kwargs: Any,
    ) -> ChatMessage:
        if self._index >= len(self._actions):
            # 脚本耗尽：返回不含工具调用的消息，主循环据此停止。
            return ChatMessage(role=MessageRole.ASSISTANT, content=self.exhausted_content, tool_calls=None)

        action = self._actions[self._index]
        self._index += 1
        call_id = action.resolved_id(f"call_{self._index}")
        tool_call = ChatMessageToolCall(
            id=call_id,
            type="function",
            function=ChatMessageToolCallFunction(name=action.tool_name, arguments=dict(action.arguments)),
        )
        return ChatMessage(role=MessageRole.ASSISTANT, content="", tool_calls=[tool_call])

    def parse_tool_calls(self, message: ChatMessage) -> ChatMessage:  # type: ignore[override]
        """脚本模型直接产出结构化工具调用；无调用时原样返回（不抛异常）。"""

        return message
