"""state_store.py —— 运行状态、预算、证据、审批与步骤记录（可序列化，供 Checkpoint 恢复）。

设计原则：
- ``RunState`` 是一次运行的**全部真相**：任务、类型、状态、停止原因、步骤、证据、审批、
  待审批动作、预算与工作区指纹；可 ``to_dict()`` / ``from_dict()`` 无损往返（供 checkpoint）。
- ``StateStore`` 是对 ``RunState`` 的**受控修改入口**：登记步骤/证据、消费预算、授予/消费/吊销审批、
  设置待审批动作、幂等查询等；不含任何“是否放行”的策略（那是 gate.py 的职责）。
- 内容寻址：``args_digest(tool, args)`` 生成稳定的幂等键，用于 G4 幂等与审批匹配。

本模块是纯 Python、无 smolagents 依赖，可独立单测。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


# --------------------------------------------------------------------------- #
# 枚举（StrEnum：其成员与对应字符串相等且同哈希，便于 JSON 往返与集合判定）
# --------------------------------------------------------------------------- #
class TaskKind(StrEnum):
    CODE_UNDERSTANDING = "code_understanding"
    TEST_REPAIR = "test_repair"


class RunStatus(StrEnum):
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    STOPPED = "stopped"


class StopReason(StrEnum):
    COMPLETED = "completed"
    WAITING_APPROVAL = "waiting_approval"
    MAX_STEPS = "max_steps"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INTERRUPTED = "interrupted"
    MODEL_ERROR = "model_error"
    WORKSPACE_CHANGED = "workspace_changed"
    UNKNOWN_RESULT = "unknown_result"


class EvidenceKind(StrEnum):
    FILE_READ = "file_read"
    LIST_DIR = "list_dir"
    DELIVERABLE = "deliverable"
    TEST_RESULT = "test_result"


class ResultStatus(StrEnum):
    PENDING = "pending"      # 写意图已登记、结果未知（崩溃恢复的关键状态）
    SUCCESS = "success"      # handler 执行成功
    FAILED = "failed"        # handler 执行抛错
    DENIED = "denied"        # 被门禁拒绝，未进入 handler


# --------------------------------------------------------------------------- #
# 内容寻址幂等键
# --------------------------------------------------------------------------- #
def args_digest(tool_name: str, arguments: dict[str, Any] | None) -> str:
    """对 (工具名, 参数) 生成稳定的内容摘要，用作幂等键与审批匹配键。"""

    payload = json.dumps(
        {"tool": tool_name, "args": arguments or {}},
        sort_keys=True, ensure_ascii=False, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# 预算
# --------------------------------------------------------------------------- #
@dataclass
class Budget:
    """步数 / 写入次数 / 墙钟时间预算。"""

    max_steps: int = 12
    max_writes: int = 8
    max_seconds: float = 600.0
    steps_used: int = 0
    writes_used: int = 0
    started_at: float = field(default_factory=time.time)

    def seconds_used(self) -> float:
        return time.time() - self.started_at

    def steps_exhausted(self) -> bool:
        return self.steps_used >= self.max_steps

    def writes_exhausted(self) -> bool:
        return self.writes_used >= self.max_writes

    def seconds_exhausted(self) -> bool:
        return self.seconds_used() >= self.max_seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_steps": self.max_steps,
            "max_writes": self.max_writes,
            "max_seconds": self.max_seconds,
            "steps_used": self.steps_used,
            "writes_used": self.writes_used,
            "started_at": self.started_at,
            "seconds_used": round(self.seconds_used(), 3),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Budget":
        return cls(
            max_steps=data.get("max_steps", 12),
            max_writes=data.get("max_writes", 8),
            max_seconds=data.get("max_seconds", 600.0),
            steps_used=data.get("steps_used", 0),
            writes_used=data.get("writes_used", 0),
            started_at=data.get("started_at", time.time()),
        )


# --------------------------------------------------------------------------- #
# 证据
# --------------------------------------------------------------------------- #
@dataclass
class Evidence:
    """一条证据：文件阅读 / 目录列表 / 交付物 / 测试结果。

    ``fingerprint`` 绑定“证据产生时的工作区指纹”；源码变更后旧证据即失效（H2/C4）。
    ``invalidated`` 由 ``StateStore.invalidate_all_evidence`` 置位（工作区变化时）。
    """

    kind: EvidenceKind | str
    ref: str
    detail: dict[str, Any]
    fingerprint: str
    step_number: int
    tool_call_id: str | None = None
    invalidated: bool = False
    invalidated_reason: str = ""
    created_at: float = field(default_factory=time.time)

    def is_fresh(self, current_fingerprint: str) -> bool:
        """证据是否“在当前工作区版本上仍然有效”。"""

        return (not self.invalidated) and self.fingerprint == current_fingerprint

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "ref": self.ref,
            "detail": dict(self.detail),
            "fingerprint": self.fingerprint,
            "step_number": self.step_number,
            "tool_call_id": self.tool_call_id,
            "invalidated": self.invalidated,
            "invalidated_reason": self.invalidated_reason,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Evidence":
        return cls(
            kind=EvidenceKind(data["kind"]),
            ref=data.get("ref", ""),
            detail=dict(data.get("detail", {})),
            fingerprint=data.get("fingerprint", ""),
            step_number=data.get("step_number", 0),
            tool_call_id=data.get("tool_call_id"),
            invalidated=data.get("invalidated", False),
            invalidated_reason=data.get("invalidated_reason", ""),
            created_at=data.get("created_at", time.time()),
        )


# --------------------------------------------------------------------------- #
# 步骤记录
# --------------------------------------------------------------------------- #
@dataclass
class StepRecord:
    """一次工具调用的落库记录（供幂等、恢复与记忆重建）。"""

    step_number: int
    tool_call_id: str
    tool_name: str
    args_digest: str = ""
    decision: str = ""
    rule: str = ""
    result_status: ResultStatus | str = ResultStatus.SUCCESS
    is_write: bool = False
    idempotency_key: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    observation: str | None = None
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_number": self.step_number,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "args_digest": self.args_digest,
            "decision": self.decision,
            "rule": self.rule,
            "result_status": str(self.result_status),
            "is_write": self.is_write,
            "idempotency_key": self.idempotency_key,
            "arguments": dict(self.arguments),
            "observation": self.observation,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StepRecord":
        return cls(
            step_number=data.get("step_number", 0),
            tool_call_id=data.get("tool_call_id", ""),
            tool_name=data.get("tool_name", ""),
            args_digest=data.get("args_digest", ""),
            decision=data.get("decision", ""),
            rule=data.get("rule", ""),
            result_status=ResultStatus(data.get("result_status", "success")),
            is_write=data.get("is_write", False),
            idempotency_key=data.get("idempotency_key", ""),
            arguments=dict(data.get("arguments", {})),
            observation=data.get("observation"),
            started_at=data.get("started_at", time.time()),
            ended_at=data.get("ended_at"),
        )


# --------------------------------------------------------------------------- #
# 审批
# --------------------------------------------------------------------------- #
@dataclass
class Approval:
    """一次授权：绑定 (工具名, 参数摘要)；一次性消费；工作区变化可吊销。"""

    approval_id: str
    tool_name: str
    args_digest: str
    actor: str = "user"
    granted_at: float = field(default_factory=time.time)
    consumed: bool = False
    revoked: bool = False
    revoked_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "tool_name": self.tool_name,
            "args_digest": self.args_digest,
            "actor": self.actor,
            "granted_at": self.granted_at,
            "consumed": self.consumed,
            "revoked": self.revoked,
            "revoked_reason": self.revoked_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Approval":
        return cls(
            approval_id=data.get("approval_id", ""),
            tool_name=data.get("tool_name", ""),
            args_digest=data.get("args_digest", ""),
            actor=data.get("actor", "user"),
            granted_at=data.get("granted_at", time.time()),
            consumed=data.get("consumed", False),
            revoked=data.get("revoked", False),
            revoked_reason=data.get("revoked_reason", ""),
        )


# --------------------------------------------------------------------------- #
# 运行状态（一次运行的全部真相）
# --------------------------------------------------------------------------- #
@dataclass
class RunState:
    run_id: str
    task: str = ""
    task_kind: TaskKind | str = TaskKind.CODE_UNDERSTANDING
    workspace_root: str = ""
    budget: Budget = field(default_factory=Budget)
    status: RunStatus | str = RunStatus.RUNNING
    stop_reason: StopReason | str | None = None
    steps: list[StepRecord] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    approvals: list[Approval] = field(default_factory=list)
    pending_action: dict[str, Any] | None = None
    final_answer: str | None = None
    workspace_fingerprint: str | None = None
    # 连续重复动作计数（G2）：同一 (工具, 参数) 连续请求的次数。
    last_request_digest: str | None = None
    repeat_count: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task": self.task,
            "task_kind": str(self.task_kind),
            "workspace_root": self.workspace_root,
            "budget": self.budget.to_dict(),
            "status": str(self.status),
            "stop_reason": str(self.stop_reason) if self.stop_reason is not None else None,
            "steps": [s.to_dict() for s in self.steps],
            "evidence": [e.to_dict() for e in self.evidence],
            "approvals": [a.to_dict() for a in self.approvals],
            "pending_action": dict(self.pending_action) if self.pending_action else None,
            "final_answer": self.final_answer,
            "workspace_fingerprint": self.workspace_fingerprint,
            "last_request_digest": self.last_request_digest,
            "repeat_count": self.repeat_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunState":
        stop_reason = data.get("stop_reason")
        return cls(
            run_id=data.get("run_id", ""),
            task=data.get("task", ""),
            task_kind=data.get("task_kind", TaskKind.CODE_UNDERSTANDING),
            workspace_root=data.get("workspace_root", ""),
            budget=Budget.from_dict(data.get("budget", {})),
            status=data.get("status", RunStatus.RUNNING),
            stop_reason=stop_reason if stop_reason is None else stop_reason,
            steps=[StepRecord.from_dict(s) for s in data.get("steps", [])],
            evidence=[Evidence.from_dict(e) for e in data.get("evidence", [])],
            approvals=[Approval.from_dict(a) for a in data.get("approvals", [])],
            pending_action=data.get("pending_action"),
            final_answer=data.get("final_answer"),
            workspace_fingerprint=data.get("workspace_fingerprint"),
            last_request_digest=data.get("last_request_digest"),
            repeat_count=data.get("repeat_count", 0),
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
        )


# --------------------------------------------------------------------------- #
# 受控状态修改入口
# --------------------------------------------------------------------------- #
class StateStore:
    """对 ``RunState`` 的受控读写；不含放行策略（策略在 gate.py）。"""

    def __init__(self, state: RunState) -> None:
        self.state = state

    # ---- 便捷属性 ----
    @property
    def run_id(self) -> str:
        return self.state.run_id

    # ---- 状态 / 预算 ----
    def set_status(self, status: RunStatus | str, stop_reason: StopReason | str | None = None) -> None:
        self.state.status = status
        self.state.stop_reason = stop_reason
        self.state.updated_at = time.time()

    def consume_step(self) -> None:
        """主循环每推进一步调用一次：步数预算 +1。"""

        self.state.budget.steps_used += 1
        self.state.updated_at = time.time()

    def record_write(self) -> None:
        self.state.budget.writes_used += 1
        self.state.updated_at = time.time()

    # ---- 步骤 ----
    def add_step(self, record: StepRecord) -> StepRecord:
        self.state.steps.append(record)
        self.state.updated_at = time.time()
        return record

    def last_step(self) -> StepRecord | None:
        return self.state.steps[-1] if self.state.steps else None

    def has_executed(self, idempotency_key: str) -> bool:
        """幂等：某个写动作（按内容摘要）是否已成功执行过（G4）。"""

        if not idempotency_key:
            return False
        for step in self.state.steps:
            if step.idempotency_key == idempotency_key and str(step.result_status) == str(ResultStatus.SUCCESS):
                return True
        return False

    # ---- 证据 ----
    def add_evidence(self, evidence: Evidence) -> Evidence:
        self.state.evidence.append(evidence)
        self.state.updated_at = time.time()
        return evidence

    def invalidate_all_evidence(self, reason: str) -> int:
        """工作区变化时使全部既有证据失效（C4 / H2）。返回受影响条数。"""

        count = 0
        for ev in self.state.evidence:
            if not ev.invalidated:
                ev.invalidated = True
                ev.invalidated_reason = reason
                count += 1
        return count

    def evidence_of(self, kind: EvidenceKind | str) -> list[Evidence]:
        return [e for e in self.state.evidence if str(e.kind) == str(kind)]

    # ---- 审批 ----
    def grant_approval(self, approval_id: str, tool_name: str, digest: str,
                       *, actor: str = "user") -> Approval:
        approval = Approval(approval_id=approval_id, tool_name=tool_name,
                            args_digest=digest, actor=str(actor))
        self.state.approvals.append(approval)
        self.state.updated_at = time.time()
        return approval

    def find_approval(self, tool_name: str, digest: str, *, active_only: bool = True) -> Approval | None:
        """查找匹配的未吊销审批；``active_only`` 时排除已消费者。"""

        for ap in self.state.approvals:
            if ap.revoked or ap.tool_name != tool_name or ap.args_digest != digest:
                continue
            if active_only and ap.consumed:
                continue
            return ap
        return None

    def consume_approval(self, approval_id: str) -> bool:
        for ap in self.state.approvals:
            if ap.approval_id == approval_id and not ap.consumed and not ap.revoked:
                ap.consumed = True
                self.state.updated_at = time.time()
                return True
        return False

    def revoke_approvals(self, reason: str) -> int:
        """吊销所有未消费的审批（工作区变化后旧授权不再可信，C4）。"""

        count = 0
        for ap in self.state.approvals:
            if not ap.revoked and not ap.consumed:
                ap.revoked = True
                ap.revoked_reason = reason
                count += 1
        return count

    # ---- 待审批动作 ----
    def set_pending_action(self, tool_name: str, digest: str, tool_call_id: str,
                           *, arguments: dict[str, Any] | None = None) -> None:
        self.state.pending_action = {
            "tool_name": tool_name,
            "args_digest": digest,
            "tool_call_id": tool_call_id,
            "arguments": dict(arguments or {}),
        }
        self.state.updated_at = time.time()

    def clear_pending_action(self) -> None:
        self.state.pending_action = None

    # ---- 连续重复计数（G2）----
    def register_request(self, digest: str) -> int:
        """登记一次请求，返回“当前连续相同请求”的次数。"""

        if self.state.last_request_digest == digest:
            self.state.repeat_count += 1
        else:
            self.state.last_request_digest = digest
            self.state.repeat_count = 1
        return self.state.repeat_count
