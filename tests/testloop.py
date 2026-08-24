"""单轮决策 Loop 验证脚本：参照本项目的适配器体系改造。

- 决策器使用 ModelAdapter 抽象接口，通过 MODEL_PROVIDER 选择具体适配器
- 结构化输出走 output_schema（json_mode），由适配器统一解析
- llama 为本地部署，无需 API Key；deepseek 凭据从 .env 读取

运行方式（项目根目录）：python tests/testloop.py
"""
import sys
from pathlib import Path
from typing import Literal

# 支持直接 python tests/testloop.py 运行：将项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import BaseModel, Field

from src.adapter.dsAdapter import DeepSeekChatAdapter
from src.adapter.llamaAdapter import LlamaCppChatAdapter
from src.adapter.modelAdapter import ModelAdapter, ModelRequest


class AgentAction(BaseModel):
    """编码 Agent 单步动作的结构化输出 schema。"""
    action: Literal["inspect", "edit", "run_tests", "finish"] = Field(
        description="下一步要执行的动作类型"
    )
    target: str | None = Field(
        default=None, description="动作对象（如文件路径或测试命令），finish 时可为空"
    )
    reason: str = Field(description="选择该动作的简短理由")


class OneTurnLoop:
    """单轮决策循环：向模型请求一次结构化动作并校验返回。"""

    def __init__(self, model: ModelAdapter) -> None:
        self.model = model

    def run(self, goal: str) -> AgentAction:
        request = ModelRequest(
            prompt_template="coding_agent_decision",  # 受控模板库登记的系统提示词
            user=goal,
            max_output_tokens=1024,
            output_schema=AgentAction.model_json_schema(),
        )

        result = self.model.generate(request)
        if result.kind != "structured" or result.data is None:
            raise RuntimeError("模型没有返回结构化动作")

        # Adapter 负责协议归一化，Loop 仍负责领域对象校验。
        return AgentAction.model_validate(result.data)


if __name__ == "__main__":
    # 适配器选择：llama 走本地部署（默认），deepseek 走云端接口
    provider = "llama"

    if provider == "llama":
        adapter: ModelAdapter = LlamaCppChatAdapter()
    elif provider == "deepseek":
        adapter = DeepSeekChatAdapter()
    else:
        raise ValueError(f"未知 MODEL_PROVIDER: {provider}")

    loop = OneTurnLoop(adapter)
    action = loop.run("检查 tests/adapter_test.py 失败原因，当前尚未运行测试。")
    print(action)
