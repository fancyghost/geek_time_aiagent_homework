"""catalog.py —— 工具目录：规格、严格参数模型与“哑” handler。

设计原则（对应验收“业务规则不散落在 handler”）：
- handler 只做机械副作用（读/写/列目录/跑命令），并产出结构化结果与证据；
- 所有治理（参数、权限、风险、审批、重复、幂等、路径/命令边界）都在 gate.py / sandbox.py；
- 每个工具的“契约”集中在 ``ToolSpec``：参数模型、策略、handler、路径/命令字段、证据类型。

这样 Gate 只需要读 ``ToolSpec`` 就能完成全部前置判定，handler 不含任何门禁逻辑。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sandbox import Sandbox
from state_store import Evidence, EvidenceKind, StateStore


# --------------------------------------------------------------------------- #
# 策略枚举
# --------------------------------------------------------------------------- #
class Effect(StrEnum):
    READ = "read"
    WRITE = "write"
    SHELL = "shell"


class Risk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# --------------------------------------------------------------------------- #
# 严格参数模型（pydantic v2）：extra=forbid 拒绝模型注入的额外字段（G1）
# --------------------------------------------------------------------------- #
class StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadFileArgs(StrictArgs):
    path: str = Field(min_length=1, description="工作区内相对/绝对路径")


class ListDirArgs(StrictArgs):
    path: str = Field(default=".", description="要列出的目录，默认工作区根")


class WriteFileArgs(StrictArgs):
    path: str = Field(min_length=1)
    content: str = Field(description="写入的完整文本内容")


class RunTestArgs(StrictArgs):
    command: str = Field(min_length=1, description="测试命令，必须在白名单内")


class FinalAnswerArgs(StrictArgs):
    answer: str = Field(description="给用户的最终答案/结论")


# --------------------------------------------------------------------------- #
# handler 运行期依赖与产出
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class HandlerDeps:
    """handler 可用到的受控依赖：沙箱、状态、运行上下文与指纹计算。"""

    sandbox: Sandbox
    state: StateStore
    run_id: str
    step_number: int
    tool_call_id: str
    fingerprint: Callable[[], str]
    timeout_seconds: float | None = None


@dataclass(slots=True)
class HandlerOutcome:
    """handler 的产出：给模型看的结果 + 需要登记的证据 + 是否为写操作。"""

    result: dict[str, Any]
    evidence: Evidence | None = None
    is_write: bool = False


Handler = Callable[[StrictArgs, HandlerDeps], HandlerOutcome]


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    effect: Effect
    risk: Risk
    requires_approval: bool
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[StrictArgs]
    policy: ToolPolicy
    handler: Handler
    # 供审计展示的“人类可读目标”（路径 / 命令 / 答案预览）
    canonical_target: Callable[[StrictArgs], str]
    # 需要经沙箱路径边界校验的字段
    path_fields: tuple[str, ...] = ()
    # 需要经命令白名单校验的字段
    command_field: str | None = None
    # 该工具产出的证据类型（None 表示不产证据，如 final_answer）
    evidence_kind: EvidenceKind | None = None
    # 模型可见的输入 schema（不含系统注入的 tool_call_id）
    model_inputs: dict[str, dict[str, str]] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 具体 handler：只做机械副作用 + 产出证据，无任何门禁判断
# --------------------------------------------------------------------------- #
def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_file_handler(args: ReadFileArgs, deps: HandlerDeps) -> HandlerOutcome:
    text, resolved = deps.sandbox.read_text(args.path)
    evidence = Evidence(
        kind=EvidenceKind.FILE_READ,
        ref=resolved.rel,
        detail={"chars": len(text), "sha256": _sha(text)},
        fingerprint=deps.fingerprint(),
        step_number=deps.step_number,
        tool_call_id=deps.tool_call_id,
    )
    preview = text[:400] + ("..." if len(text) > 400 else "")
    return HandlerOutcome(
        result={"path": resolved.rel, "chars": len(text), "preview": preview},
        evidence=evidence,
    )


def list_dir_handler(args: ListDirArgs, deps: HandlerDeps) -> HandlerOutcome:
    entries, resolved = deps.sandbox.list_dir(args.path)
    evidence = Evidence(
        kind=EvidenceKind.LIST_DIR,
        ref=resolved.rel,
        detail={"count": len(entries)},
        fingerprint=deps.fingerprint(),
        step_number=deps.step_number,
        tool_call_id=deps.tool_call_id,
    )
    return HandlerOutcome(result={"path": resolved.rel, "entries": entries}, evidence=evidence)


def write_file_handler(args: WriteFileArgs, deps: HandlerDeps) -> HandlerOutcome:
    resolved = deps.sandbox.write_text(args.path, args.content)
    # 交付物证据的指纹在“写入之后”计算，反映最新工作区状态。
    evidence = Evidence(
        kind=EvidenceKind.DELIVERABLE,
        ref=resolved.rel,
        detail={"bytes": len(args.content.encode("utf-8")), "sha256": _sha(args.content)},
        fingerprint=deps.fingerprint(),
        step_number=deps.step_number,
        tool_call_id=deps.tool_call_id,
    )
    return HandlerOutcome(
        result={"path": resolved.rel, "bytes": len(args.content)},
        evidence=evidence,
        is_write=True,
    )


def run_test_handler(args: RunTestArgs, deps: HandlerDeps) -> HandlerOutcome:
    cmd = deps.sandbox.run_command(args.command, timeout_seconds=deps.timeout_seconds)
    passed = (cmd.returncode == 0) and not cmd.timed_out
    evidence = Evidence(
        kind=EvidenceKind.TEST_RESULT,
        ref=args.command,
        detail={
            "returncode": cmd.returncode,
            "passed": passed,
            "timed_out": cmd.timed_out,
            "terminated": cmd.terminated,
            "elapsed_ms": cmd.elapsed_ms,
            "stdout_tail": cmd.stdout[-800:],
            "stderr_tail": cmd.stderr[-400:],
        },
        # 关键：测试结果证据绑定“被测源码”的当前指纹，源码变更即失效（H2）。
        fingerprint=deps.fingerprint(),
        step_number=deps.step_number,
        tool_call_id=deps.tool_call_id,
    )
    return HandlerOutcome(
        result={
            "command": args.command,
            "returncode": cmd.returncode,
            "passed": passed,
            "timed_out": cmd.timed_out,
            "stdout_tail": cmd.stdout[-800:],
            "stderr_tail": cmd.stderr[-400:],
        },
        evidence=evidence,
    )


def final_answer_handler(args: FinalAnswerArgs, deps: HandlerDeps) -> HandlerOutcome:
    # final_answer 不产生副作用、不产证据；是否“可完成”由 completion.py 判定。
    return HandlerOutcome(result={"answer": args.answer})


# --------------------------------------------------------------------------- #
# 工具目录
# --------------------------------------------------------------------------- #
def build_specs() -> dict[str, ToolSpec]:
    """构造两类仓库任务所需的工具规格集合。"""

    specs = [
        ToolSpec(
            name="read_file",
            description="读取工作区内某个文件的文本内容（只读）。",
            args_model=ReadFileArgs,
            policy=ToolPolicy(Effect.READ, Risk.LOW, requires_approval=False, timeout_seconds=2.0),
            handler=read_file_handler,
            canonical_target=lambda a: str(getattr(a, "path")),
            path_fields=("path",),
            evidence_kind=EvidenceKind.FILE_READ,
            model_inputs={"path": {"type": "string", "description": "工作区内文件路径"}},
        ),
        ToolSpec(
            name="list_dir",
            description="列出工作区内某个目录的条目（只读）。",
            args_model=ListDirArgs,
            policy=ToolPolicy(Effect.READ, Risk.LOW, requires_approval=False, timeout_seconds=2.0),
            handler=list_dir_handler,
            canonical_target=lambda a: str(getattr(a, "path")),
            path_fields=("path",),
            evidence_kind=EvidenceKind.LIST_DIR,
            model_inputs={"path": {"type": "string", "description": "工作区内目录路径，默认 ."}},
        ),
        ToolSpec(
            name="write_file",
            description="向工作区内某个文件写入完整内容（写操作，需审批）。",
            args_model=WriteFileArgs,
            policy=ToolPolicy(Effect.WRITE, Risk.HIGH, requires_approval=True, timeout_seconds=2.0),
            handler=write_file_handler,
            canonical_target=lambda a: str(getattr(a, "path")),
            path_fields=("path",),
            evidence_kind=EvidenceKind.DELIVERABLE,
            model_inputs={
                "path": {"type": "string", "description": "工作区内文件路径"},
                "content": {"type": "string", "description": "写入的完整文本内容"},
            },
        ),
        ToolSpec(
            name="run_test",
            description="在白名单与超时约束内运行测试命令，返回是否通过。",
            args_model=RunTestArgs,
            policy=ToolPolicy(Effect.SHELL, Risk.MEDIUM, requires_approval=False, timeout_seconds=5.0),
            handler=run_test_handler,
            canonical_target=lambda a: str(getattr(a, "command")),
            command_field="command",
            evidence_kind=EvidenceKind.TEST_RESULT,
            model_inputs={"command": {"type": "string", "description": "测试命令，必须在白名单内"}},
        ),
        ToolSpec(
            name="final_answer",
            description="在满足交付物与证据要求后，给出最终答案。",
            args_model=FinalAnswerArgs,
            policy=ToolPolicy(Effect.READ, Risk.LOW, requires_approval=False, timeout_seconds=1.0),
            handler=final_answer_handler,
            canonical_target=lambda _a: "final_answer",
            evidence_kind=None,
            model_inputs={"answer": {"type": "string", "description": "最终答案/结论"}},
        ),
    ]
    return {spec.name: spec for spec in specs}
