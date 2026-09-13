"""工具消息类型：模型侧的 Tool Call 与执行后的 Tool Result。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolCall:
    """模型返回的一次工具调用（已归一化为纯数据）。"""
    id: str                  # 工具调用 ID，回写结果时原样带回
    name: str                # 工具名
    arguments_json: str      # 原始 JSON 字符串参数，执行时才解析


@dataclass
class ToolResult:
    """一次工具执行的结果，可转为 OpenAI tool 角色消息回写对话历史。"""
    call_id: str
    name: str
    content: str             # 执行产出的文本内容（成功为数据，失败为错误说明）
    ok: bool = True          # 是否执行成功

    def to_model_message(self) -> dict:
        """转为 chat completions 的 tool 角色消息（tool_call_id 与请求对应）。"""
        return {
            "role": "tool",
            "tool_call_id": self.call_id,
            "content": self.content,
        }
