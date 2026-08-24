"""模型适配器抽象层：定义统一的模型请求/响应结构与适配器接口。"""
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from src.adapter.promptTemplates import get_system_prompt

# 模型返回结果的类型标识
ResultKind = Literal["text", "structured", "tool_calls", "refusal"]

# 可调用的接口类型标识：chat completions / responses（OpenAI 两代对话接口）
ApiMode = Literal["chat_completions", "responses"]

# 系统提示词解析钩子：默认从受控模板库按名解析（latest 版本）；
# 网关层会替换为"版本化模板 + 变量渲染"的实现（见 src/gateway.py）
SystemResolver = Callable[[str], str]
_system_resolver: SystemResolver = get_system_prompt


def set_system_resolver(resolver: SystemResolver) -> None:
    """替换系统提示词解析实现（网关启动时装配版本化渲染器）。"""
    global _system_resolver
    _system_resolver = resolver


def resolve_system(prompt_template: str) -> str:
    """按当前装配的解析器解析系统提示词。"""
    return _system_resolver(prompt_template)

@dataclass(frozen=True)
class ModelCapabilities:
    """模型能力声明，用于上层根据能力选择适配器。"""
    chat_completions: bool      # 是否支持 chat completions 接口
    responses: bool             # 是否支持 responses 接口
    structured_output: Literal["native_schema", "json_mode", "prompt_only"]  # 结构化输出支持程度
    tool_calling: bool          # 是否支持工具调用
    supports_temperature: bool  # 是否支持 temperature 参数
    supports_top_p: bool        # 是否支持 top_p 参数

@dataclass(frozen=True)
class ModelRequest:
    """统一的模型调用请求参数。

    安全约定：系统提示词必须来自受控模板库，禁止外部自由文本。
    - prompt_template：模板引用（适配层默认通道，取 latest 版本）
    - system_text：已渲染的系统提示词，仅限网关等受控代码在模板渲染后注入，
      HTTP API 不暴露该字段
    """
    user: str                                    # 用户输入（不可信数据，仅作为对话内容）
    prompt_template: str | None = "general_chat"  # 系统提示词模板名，必须已登记
    system_text: str | None = None               # 受控注入的已渲染系统提示词（与模板名二选一）
    max_output_tokens: int = 1024                # 最大输出 token 数
    temperature: float | None = None             # 采样温度，None 表示使用模型默认值
    top_p: float | None = None                   # 核采样参数，None 表示使用模型默认值
    output_schema: dict[str, Any] | None = None  # 期望的结构化输出 JSON Schema，None 表示纯文本输出
    tools: list[dict[str, Any]] | None = None    # 可供模型调用的工具定义（OpenAI 格式），None 表示不启用工具调用
    api_mode: ApiMode | None = None              # 指定调用接口；None 表示由适配器按自身能力默认选择（chat_completions）

    def __post_init__(self) -> None:
        # 构造时即校验：模板名与渲染文本必须且只能提供一个，把注入拦截在请求入口
        if self.system_text is not None and self.prompt_template is not None:
            raise ValueError("system_text 与 prompt_template 只能二选一")
        if self.system_text is None and self.prompt_template is None:
            raise ValueError("必须提供 prompt_template 或受控渲染的 system_text")
        if self.prompt_template is not None:
            resolve_system(self.prompt_template)  # 未登记模板直接报错

    @property
    def system(self) -> str:
        """解析系统提示词：优先受控注入的渲染文本，否则按模板名解析。"""
        if self.system_text is not None:
            return self.system_text
        return resolve_system(self.prompt_template)  # type: ignore[arg-type]

@dataclass
class ModelResult:
    """统一的模型调用返回结果。"""
    kind: ResultKind                             # 结果类型
    text: str | None = None                      # 文本输出（结构化时为原始 JSON 字符串）
    data: dict[str, Any] | None = None           # 结构化输出解析后的数据
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # 工具调用列表
    finish_reason: str | None = None             # 结束原因（如 stop/length）
    input_tokens: int = 0                        # 输入 token 消耗
    output_tokens: int = 0                       # 输出 token 消耗
    cached_tokens: int = 0                       # 命中上下文缓存的输入 token（分类统计）
    reasoning_tokens: int = 0                    # 思维链 token（分类统计）
    request_id: str | None = None                # 服务端请求 ID，便于排查问题

@dataclass
class StreamChunk:
    """流式输出的单个块：文本增量 / 结束标记 / 用量。"""
    text_delta: str = ""                         # 文本增量（空表示非文本事件）
    finish_reason: str | None = None             # 最后一个块携带结束原因
    usage: ModelResult | None = None             # 最后一个块携带完整用量统计
    tool_calls: list[dict[str, Any]] | None = None  # 结束块可携带工具调用（如有）

class ModelAdapter(ABC):
    """模型适配器抽象基类，各模型厂商适配器需实现 generate 方法。"""
    name: str                      # 适配器对应的模型名
    capabilities: ModelCapabilities  # 模型能力声明

    def __init__(self) -> None:
        # 故障注入钩子：attempt（0 起）-> 抛异常或 None；仅测试/验证使用
        self._fault_hook: Callable[[int], None] | None = None

    def set_fault_hook(self, hook: Callable[[int], None] | None) -> None:
        """设置/清除故障注入钩子（重试验证的故障注入点）。"""
        self._fault_hook = hook

    def _maybe_inject_fault(self, attempt: int) -> None:
        """generate/generate_stream 起始处调用，触发已注入的故障。"""
        if self._fault_hook is not None:
            self._fault_hook(attempt)

    def resolve_api_mode(self, request: ModelRequest) -> ApiMode:
        """协商调用接口：显式指定时校验适配器能力，不支持则报错；未指定默认 chat_completions。"""
        mode: ApiMode = request.api_mode or "chat_completions"
        if mode == "responses" and not self.capabilities.responses:
            raise ValueError(f"适配器 {self.name} 不支持 responses 接口")
        if mode == "chat_completions" and not self.capabilities.chat_completions:
            raise ValueError(f"适配器 {self.name} 不支持 chat_completions 接口")
        return mode

    @abstractmethod
    def generate(self, request: ModelRequest) -> ModelResult:
        """执行一次模型调用，返回统一格式的结果。"""
        raise NotImplementedError

    def generate_stream(self, request: ModelRequest) -> Iterator[StreamChunk]:
        """流式调用：逐块 yield StreamChunk；未实现的适配器抛 NotImplementedError。"""
        raise NotImplementedError(f"适配器 {self.name} 未实现流式输出")
