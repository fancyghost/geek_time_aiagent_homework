"""适配器测试场景：场景1、3 分别由不同适配器实现。

- 场景1（纯文本问答）：QoderChatAdapter（纯问答模式）
- 场景2（Responses 接口）：DeepSeekChatAdapter（api_mode="responses"）
- 场景3（工具调用）：DeepSeekChatAdapter（支持 tools 传递与解析）
- 场景4（本地模型）：LlamaCppChatAdapter（llama.cpp 本地部署，无需 API Key）

注意：DeepSeek / Qoder 场景会真实调用外部服务（分别消耗
DeepSeek token 和 Qoder credits），需 .env 中已配置对应凭据；
llama 场景需本地 llama.cpp server 已启动（默认 http://localhost:8080/v1）。
"""
import sys
from pathlib import Path

# 支持直接 python tests/adapter_test.py 运行：将项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.adapter.dsAdapter import DeepSeekChatAdapter
from src.adapter.llamaAdapter import LlamaCppChatAdapter
from src.adapter.modelAdapter import ModelRequest
from src.adapter.qoderAdapter import QoderChatAdapter


@pytest.fixture(scope="module")
def qoder_adapter() -> QoderChatAdapter:
    """Qoder 纯问答适配器（配置从 config 读取）。"""
    return QoderChatAdapter()


@pytest.fixture(scope="module")
def deepseek_adapter() -> DeepSeekChatAdapter:
    """DeepSeek 适配器（配置从 config 读取）。"""
    return DeepSeekChatAdapter()


@pytest.fixture(scope="module")
def llama_adapter() -> LlamaCppChatAdapter:
    """llama.cpp 本地模型适配器（配置从 config 读取，无需 API Key）。"""
    return LlamaCppChatAdapter()

# 注：fixture 不允许直接调用（pytest 9.x 会报错），直接构造适配器实例
#LLM = QoderChatAdapter()
LLM = DeepSeekChatAdapter()
LLAMA = LlamaCppChatAdapter()

def test_plain_text_without_tools(deepseek_adapter) -> None:
    """场景1：不传 tools、不传 schema 的普通文本调用，应返回纯文本结果。"""
    request = ModelRequest(user="用一句话介绍北京")
    result = deepseek_adapter.generate(request)
    print(result)

    assert result.kind == "text"
    assert result.text                    # 非空文本
    assert result.data is None            # 无结构化数据
    assert result.tool_calls == []        # 纯问答模式，不应有工具调用
    # 注：Qoder 运行时对部分模型返回的 token 数恒为 0（计费口径为 credits），
    # 因此此处不断言 token 用量；finish_reason 取决于运行时，亦不强断言


def test_deepseek_responses_plain_text(deepseek_adapter) -> None:
    """场景2a：DeepSeek Responses 接口纯文本调用，应返回纯文本结果。"""
    request = ModelRequest(
        user="用一句话介绍北京",
        api_mode="responses",
    )
    result = deepseek_adapter.generate(request)
    print(result)

    assert result.kind == "text"
    assert result.text                    # 非空文本
    assert result.data is None            # 无结构化数据
    assert result.tool_calls == []        # 未传 tools，不应有工具调用
    assert result.finish_reason == "stop"  # responses status=completed 映射为 stop
    assert result.input_tokens > 0        # responses 接口返回 input_tokens 用量
    assert result.output_tokens > 0


def test_deepseek_responses_structured_output(deepseek_adapter) -> None:
    """场景2b：DeepSeek Responses 接口结构化输出（原生 json_schema），应返回符合 schema 的 JSON。"""
    request = ModelRequest(
        prompt_template="city_info_json",
        user="介绍北京",
        api_mode="responses",
        output_schema={
            "type": "object",
            "properties": {
                "city": {"type": "string"},
                "is_capital": {"type": "boolean"},
            },
            "required": ["city", "is_capital"],
        },
    )
    result = deepseek_adapter.generate(request)
    print(result)

    assert result.kind == "structured"
    assert isinstance(result.data, dict)  # JSON 已解析为 dict
    assert result.data.get("city")        # 包含城市名
    assert isinstance(result.data.get("is_capital"), bool)


def test_deepseek_responses_tool_calling(deepseek_adapter) -> None:
    """场景2c：DeepSeek Responses 接口工具调用，function_call 应归一化为 chat 同款结构。"""
    request = ModelRequest(
        prompt_template="db_assistant",
        user="查询 user 表的结构",
        api_mode="responses",
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "query_table_schema",
                    "description": "查询指定数据库表的结构",
                    "parameters": {
                        "type": "object",
                        "properties": {"table": {"type": "string"}},
                        "required": ["table"],
                    },
                },
            }
        ],
    )
    result = deepseek_adapter.generate(request)
    print(result)

    assert result.kind == "tool_calls"
    assert result.tool_calls                          # 至少发起一次工具调用
    call = result.tool_calls[0]
    assert call["type"] == "function"                 # 归一化后的 chat 格式
    assert call["function"]["name"] == "query_table_schema"
    assert "user" in call["function"]["arguments"]    # 参数中包含目标表名


def test_llama_plain_text() -> None:
    """场景4a：llama.cpp 本地模型纯文本调用，应返回纯文本结果。"""
    request = ModelRequest(user="用一句话介绍北京")
    result = LLAMA.generate(request)
    print(result)

    assert result.kind == "text"
    assert result.text                    # 非空文本
    assert result.data is None            # 无结构化数据
    assert result.tool_calls == []        # 未传 tools，不应有工具调用


def test_llama_structured_output() -> None:
    """场景4b：llama.cpp 本地模型结构化输出（json_mode），应返回符合 schema 的 JSON。"""
    request = ModelRequest(
        prompt_template="city_info_json",
        user="介绍北京",
        output_schema={
            "type": "object",
            "properties": {
                "city": {"type": "string"},
                "is_capital": {"type": "boolean"},
            },
            "required": ["city", "is_capital"],
        },
    )
    result = LLAMA.generate(request)
    print(result)

    assert result.kind == "structured"
    assert isinstance(result.data, dict)  # JSON 已解析为 dict
    assert result.data.get("city")        # 包含城市名
    assert isinstance(result.data.get("is_capital"), bool)


def test_llama_tool_calling() -> None:
    """场景4c：llama.cpp 本地模型工具调用，传入 tools 且问题需要工具，应发起工具调用。"""
    request = ModelRequest(
        prompt_template="db_assistant",
        user="查询 user 表的结构",
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "query_table_schema",
                    "description": "查询指定数据库表的结构",
                    "parameters": {
                        "type": "object",
                        "properties": {"table": {"type": "string"}},
                        "required": ["table"],
                    },
                },
            }
        ],
    )
    result = LLAMA.generate(request)
    print(result)

    assert result.kind == "tool_calls"
    assert result.tool_calls                          # 至少发起一次工具调用
    call = result.tool_calls[0]
    assert call["function"]["name"] == "query_table_schema"
    assert "user" in call["function"]["arguments"]    # 参数中包含目标表名


def test_unsupported_api_mode_raises() -> None:
    """场景5：对不支持 responses 接口的适配器显式指定 responses，应抛出 ValueError。"""
    request = ModelRequest(user="你好", api_mode="responses")
    with pytest.raises(ValueError, match="responses"):
        LLAMA.generate(request)


def test_unknown_prompt_template_rejected() -> None:
    """场景6（防注入）：未登记的系统提示词模板名在构造请求时即被拒绝。"""
    from src.errors import ErrorCode, GatewayError

    with pytest.raises(GatewayError) as exc_info:
        ModelRequest(user="你好", prompt_template="ignore_previous_instructions")
    assert exc_info.value.code == ErrorCode.TEMPLATE_NOT_FOUND



if __name__ == "__main__":
    test_plain_text_without_tools(LLM)
    test_deepseek_responses_plain_text(LLM)
    test_deepseek_responses_structured_output(LLM)
    test_deepseek_responses_tool_calling(LLM)
    test_unsupported_api_mode_raises()
    test_unknown_prompt_template_rejected()
    test_llama_plain_text()
    test_llama_structured_output()
    test_llama_tool_calling()
