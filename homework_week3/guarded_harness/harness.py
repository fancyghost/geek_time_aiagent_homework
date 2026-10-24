"""harness.py —— 可恢复的 Guarded Coding Harness 主循环（H1–H3 / C1–C4 编排层）。

职责（对应交付物 harness.py）：
- **装配**：sandbox / specs / state / audit / checkpoint / gate / completion / tools / agent；
- **驱动 ReAct 循环**：每步驱动 smolagents 的 ``_step_stream``，让每个工具调用都经 Gate；
- **预算与停机**：步数/写入/时间预算、会话步数上限、审批暂停、模型耗尽 → 结构化停机原因；
- **最终验收**：模型 ``final_answer`` 只是“声称完成”，由 ``CompletionChecker`` 判定是否接受（H1/H2），
  未通过则把缺口喂回模型继续（H3：预算/最大轮数由本循环兜底）；
- **可恢复**：每个安全步骤后原子落盘 Checkpoint（C1）；``resume`` 跨进程恢复且预算不清零（C3）；
  恢复时对账（C2 结果未知的写入禁止自动重放、C4 工作区变化使证据/审批失效）；
- **人工审批**：``approve_pending`` / ``preapprove`` 授予审批，写操作经 Gate 消费审批后执行（G3）。

设计原则：模型只“决策”，程序负责“门禁、执行边界、状态、审计、恢复、验收”。
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  # 必须在导入 smolagents 之前，确保其源码在 sys.path 上

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from smolagents.agents import ActionOutput, ToolOutput
from smolagents.memory import ActionStep, TaskStep, ToolCall
from smolagents.monitoring import LogLevel, Timing

from audit import AuditActor, AuditDecision, AuditLog, AuditPhase
from catalog import ToolSpec, build_specs
from checkpoint import Checkpoint, RecoveryReport
from coding_tools import GuardedToolCallingAgent, build_guarded_tools
from completion import CompletionChecker
from gate import Gate, GateContext, GateRequest
from sandbox import Sandbox, make_default_sandbox
from state_store import (
    Budget,
    RunState,
    RunStatus,
    StateStore,
    StopReason,
    TaskKind,
    args_digest,
)

# 停机原因 → 运行状态映射（未列出的默认 STOPPED）。
_STATUS_FOR_STOP: dict[str, str] = {
    str(StopReason.COMPLETED): str(RunStatus.COMPLETED),
    str(StopReason.WAITING_APPROVAL): str(RunStatus.WAITING_APPROVAL),
}


@dataclass(slots=True)
class RunOutcome:
    """一次 ``run`` / ``resume`` / ``approve_pending`` 的结果快照。"""

    run_id: str
    status: str
    stop_reason: str | None
    accepted: bool
    reason: str
    final_answer: str | None
    steps_used: int
    writes_used: int
    resumed: bool = False
    recovery: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "accepted": self.accepted,
            "reason": self.reason,
            "final_answer": self.final_answer,
            "steps_used": self.steps_used,
            "writes_used": self.writes_used,
            "resumed": self.resumed,
            "recovery": self.recovery,
        }


class GuardedHarness:
    """把治理层与 smolagents Agent 编排成一个可恢复、可审计、可审批的执行体。"""

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        model: Any,
        task_kind: TaskKind | str = TaskKind.CODE_UNDERSTANDING,
        run_id: str | None = None,
        checkpoint_dir: str | Path | None = None,
        audit_path: str | Path | None = None,
        budget: Budget | None = None,
        sandbox: Sandbox | None = None,
        specs: dict[str, ToolSpec] | None = None,
        completion: CompletionChecker | None = None,
        duplicate_threshold: int = 3,
        verbosity: LogLevel = LogLevel.OFF,
    ) -> None:
        self.workspace_root = Path(workspace_root)
        self.model = model
        self.task_kind = task_kind
        self.run_id = run_id or f"run-{int(time.time() * 1000)}-{uuid4().hex[:6]}"

        self.sandbox = sandbox or make_default_sandbox(self.workspace_root)
        self.specs = specs if specs is not None else build_specs()
        self.budget = budget or Budget()

        state = RunState(
            run_id=self.run_id,
            task="",
            task_kind=task_kind,
            workspace_root=str(self.workspace_root),
            budget=self.budget,
        )
        self.store = StateStore(state)
        self.audit = AuditLog(path=audit_path)

        harness_dir = self.workspace_root / ".harness"
        self.checkpoint = Checkpoint(
            checkpoint_dir if checkpoint_dir is not None else harness_dir / "checkpoints",
            fingerprint_fn=self.sandbox.workspace_fingerprint,
        )
        self.gate = Gate(
            self.specs, self.sandbox, self.store, self.audit,
            duplicate_threshold=duplicate_threshold,
            durable_save=lambda: self.checkpoint.save(self.store.state),
        )
        self.completion = completion or CompletionChecker()

        tools = build_guarded_tools(self.gate, self.specs)
        self.agent = GuardedToolCallingAgent(
            tools=tools, model=model,
            max_steps=self.budget.max_steps + 5, verbosity_level=verbosity,
        )
        self._recovery: RecoveryReport | None = None

    # ------------------------------------------------------------------ #
    # 便捷访问
    # ------------------------------------------------------------------ #
    @property
    def state(self) -> RunState:
        return self.store.state

    def fingerprint(self) -> str:
        return self.sandbox.workspace_fingerprint()

    def _audit_state(self, rule: str, message: str, detail: dict[str, Any] | None = None) -> None:
        self.audit.append(
            run_id=self.run_id, phase=AuditPhase.STATE, decision=AuditDecision.RECORDED,
            rule=rule, actor=AuditActor.SYSTEM, message=message, detail=detail,
        )

    # ------------------------------------------------------------------ #
    # 运行 / 恢复
    # ------------------------------------------------------------------ #
    def run(self, task: str, *, session_step_limit: int | None = None) -> RunOutcome:
        """开始一次新运行。"""

        state = self.store.state
        state.task = task
        self._recovery = None
        self.store.set_status(RunStatus.RUNNING, None)
        self._audit_state("RUN_STARTED", task[:200], {"task_kind": str(self.task_kind)})

        self.agent.memory.reset()
        self.agent.memory.steps.append(TaskStep(task=task))
        self.checkpoint.save(state)
        return self._loop(task, session_step_limit)

    def resume(self, run_id: str | None = None, *, session_step_limit: int | None = None) -> RunOutcome:
        """从 Checkpoint 恢复（跨进程/跨实例）；预算计数不清零（C3），并对账（C2/C4）。"""

        target = run_id or self.run_id
        loaded = self.checkpoint.load(target) or self.checkpoint.load_latest()
        if loaded is None:
            self._audit_state("RESUME_FAILED", f"未找到可恢复的 Checkpoint：{target!r}")
            return self._finish(StopReason.INTERRUPTED, accepted=False,
                                reason="未找到可恢复的 Checkpoint", final_answer=None)

        # 采纳已加载的状态为唯一真相，并把各组件指向它。
        self.run_id = loaded.run_id
        self.store = StateStore(loaded)
        self.gate.store = self.store
        self.gate.context = GateContext(run_id=self.run_id, step_number=0)

        report = self.checkpoint.reconcile(loaded)
        self._recovery = report
        self.audit.append(
            run_id=self.run_id, phase=AuditPhase.STATE, decision=AuditDecision.RECORDED,
            rule="RESUMED", actor=AuditActor.SYSTEM,
            message=f"从 Checkpoint 恢复：step={report.resumed_from_step}",
            detail=report.to_dict(),
        )

        # C2：存在结果未知的写入 → 禁止自动重放，停机等待人工介入。
        if report.unknown_result_tool_calls:
            return self._finish(
                StopReason.UNKNOWN_RESULT, accepted=False,
                reason=f"存在结果未知的写操作（{len(report.unknown_result_tool_calls)} 个），禁止自动重放",
                final_answer=loaded.final_answer,
            )

        # C4：工作区变化 → 证据/审批已失效，模型需在当前版本重新取证；继续运行。
        self._rebuild_memory(loaded)
        self.store.set_status(RunStatus.RUNNING, None)
        self.checkpoint.save(loaded)
        return self._loop(loaded.task, session_step_limit)

    # ------------------------------------------------------------------ #
    # 人工审批（G3）
    # ------------------------------------------------------------------ #
    def preapprove(self, tool_name: str, arguments: dict[str, Any], *,
                   actor: str = AuditActor.USER, approval_id: str | None = None) -> str:
        """在模型发起写操作**之前**预授权（按内容摘要绑定），使其无需暂停即可执行。"""

        digest = args_digest(tool_name, arguments)
        aid = approval_id or f"approval-{uuid4().hex[:8]}"
        self.store.grant_approval(aid, tool_name, digest, actor=actor)
        self.audit.append(
            run_id=self.run_id, phase=AuditPhase.APPROVAL, decision=AuditDecision.APPROVED,
            rule="APPROVAL_GRANTED", actor=actor, tool_name=tool_name,
            message=f"预授权写操作：{tool_name}", detail={"args_digest": digest, "approval_id": aid},
        )
        return aid

    def approve_pending(self, *, actor: str = AuditActor.USER, approval_id: str | None = None,
                        session_step_limit: int | None = None) -> RunOutcome:
        """批准当前待审批动作：授予审批 → 经 Gate 重放该写操作 → 继续主循环。"""

        state = self.store.state
        pending = state.pending_action
        if not pending:
            self._audit_state("APPROVE_NO_PENDING", "没有待审批的动作")
            return self._finish(state.stop_reason or StopReason.INTERRUPTED, accepted=False,
                                reason="没有待审批的动作", final_answer=state.final_answer)

        tool_name = pending["tool_name"]
        digest = pending["args_digest"]
        tool_call_id = pending["tool_call_id"]
        arguments = dict(pending.get("arguments", {}))
        aid = approval_id or f"approval-{uuid4().hex[:8]}"

        self.store.grant_approval(aid, tool_name, digest, actor=actor)
        self.audit.append(
            run_id=self.run_id, phase=AuditPhase.APPROVAL, decision=AuditDecision.APPROVED,
            rule="APPROVAL_GRANTED", actor=actor, tool_name=tool_name,
            message=f"批准待审批动作：{tool_name}", detail={"args_digest": digest, "approval_id": aid},
        )

        # 重放：把待审批的写操作重新经 Gate 执行（此时已有有效审批，Gate 会消费它）。
        self.store.set_status(RunStatus.RUNNING, None)
        step_number = state.budget.steps_used + 1
        self.gate.set_context(GateContext(run_id=self.run_id, step_number=step_number, actor=actor))
        self.gate.execute(GateRequest(tool_call_id=tool_call_id, tool_name=tool_name, arguments=arguments))
        self.store.consume_step()
        self.checkpoint.save(state)

        # 记忆以权威状态为准重建（含刚执行的写入），随后继续让模型推进。
        self._rebuild_memory(state)
        return self._loop(state.task, session_step_limit)

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    def _loop(self, task: str, session_step_limit: int | None) -> RunOutcome:
        state = self.store.state
        budget = state.budget
        limit = session_step_limit if session_step_limit is not None else budget.max_steps
        steps_this_session = 0

        while True:
            # 预算兜底（H3）。
            if budget.steps_exhausted() or budget.seconds_exhausted():
                return self._finish(StopReason.BUDGET_EXHAUSTED, accepted=False,
                                    reason="预算耗尽（步数或时间）", final_answer=state.final_answer)
            if steps_this_session >= limit:
                return self._finish(StopReason.MAX_STEPS, accepted=False,
                                    reason="达到本次会话步数上限", final_answer=state.final_answer)

            step_number = budget.steps_used + 1
            self.gate.set_context(GateContext(run_id=self.run_id, step_number=step_number, actor=AuditActor.MODEL))
            action_step = ActionStep(step_number=step_number, timing=Timing(start_time=time.time()))

            got_final = False
            final_raw: Any = None
            saw_tool_output = False
            try:
                for output in self.agent._step_stream(action_step):
                    if isinstance(output, ToolOutput):
                        saw_tool_output = True
                    elif isinstance(output, ActionOutput) and output.is_final_answer:
                        got_final = True
                        final_raw = output.output
            except Exception as exc:  # noqa: BLE001 - 模型/解析错误转为结构化停机
                action_step.error = None
                self.agent.memory.steps.append(action_step)
                self.store.consume_step()
                self.checkpoint.save(state)
                return self._finish(StopReason.MODEL_ERROR, accepted=False,
                                    reason=f"模型/解析错误：{type(exc).__name__}: {exc}",
                                    final_answer=state.final_answer)

            self.agent.memory.steps.append(action_step)
            self.store.consume_step()
            steps_this_session += 1
            self.checkpoint.save(state)

            # G3：审批暂停——写操作等待人工批准。
            if str(state.status) == str(RunStatus.WAITING_APPROVAL):
                return self._finish(StopReason.WAITING_APPROVAL, accepted=False,
                                    reason="写操作等待人工审批", final_answer=state.final_answer)

            # 模型没有产生任何动作（脚本耗尽）→ 无法继续。
            if not saw_tool_output:
                return self._finish(StopReason.MODEL_ERROR, accepted=False,
                                    reason="模型未产生任何动作（脚本耗尽）", final_answer=state.final_answer)

            # H1/H2：模型声称完成 → 由 CompletionChecker 判定是否可接受。
            if got_final:
                answer = self._decode_final_answer(final_raw)
                verdict = self.completion.evaluate(answer, state, self.fingerprint())
                if verdict.accepted:
                    state.final_answer = answer
                    return self._finish(StopReason.COMPLETED, accepted=True,
                                        reason=verdict.reason, final_answer=answer)
                self._reject_final(action_step, verdict.missing, verdict.reason)
                # 继续循环，让模型补齐缺口后重试。

    # ------------------------------------------------------------------ #
    # 循环辅助
    # ------------------------------------------------------------------ #
    def _reject_final(self, action_step: ActionStep, missing: list[str], reason: str) -> None:
        """完成检查未通过：把缺口作为 observation 喂回模型，并留审计。"""

        feedback = f"完成检查未通过，缺少：{', '.join(missing)}。请在当前工作区补齐后重新调用 final_answer。"
        action_step.observations = (action_step.observations or "") + "\n" + feedback
        self.audit.append(
            run_id=self.run_id, phase=AuditPhase.RESULT, decision=AuditDecision.DENY,
            rule="COMPLETION_REJECTED", actor=AuditActor.HARNESS, message=reason,
            detail={"missing": list(missing)},
        )
        self.checkpoint.save(self.store.state)

    def _decode_final_answer(self, raw: Any) -> str:
        """从 final_answer 工具的结构化 observation 中取出 ``content.answer``。"""

        if raw is None:
            return ""
        data = raw
        if isinstance(data, str):
            text = data.strip()
            try:
                import json

                data = json.loads(text)
            except (ValueError, TypeError):
                return text
        if isinstance(data, dict):
            content = data.get("content")
            if isinstance(content, dict):
                return str(content.get("answer", ""))
            if content is not None:
                return str(content)
        return str(data)

    def _rebuild_memory(self, state: RunState) -> None:
        """从权威的 ``RunState.steps`` 重建 Agent 记忆（按 tool_call_id 去重，保留最新结果）。"""

        self.agent.memory.reset()
        self.agent.memory.steps.append(TaskStep(task=state.task or ""))
        order: list[str] = []
        by_id: dict[str, Any] = {}
        for idx, rec in enumerate(state.steps):
            key = rec.tool_call_id or f"__rec_{idx}"
            if key not in by_id:
                order.append(key)
            by_id[key] = rec
        for i, key in enumerate(order, start=1):
            rec = by_id[key]
            tool_call = ToolCall(name=rec.tool_name, arguments=dict(rec.arguments),
                                 id=rec.tool_call_id or f"call_{i}")
            self.agent.memory.steps.append(ActionStep(
                step_number=i,
                timing=Timing(start_time=rec.started_at, end_time=rec.ended_at or rec.started_at),
                tool_calls=[tool_call],
                observations=rec.observation or "",
            ))

    def _finish(self, stop_reason: StopReason | str, *, accepted: bool, reason: str,
                final_answer: str | None) -> RunOutcome:
        """收敛为一次结构化结果：写状态、落盘、审计。"""

        state = self.store.state
        status = _STATUS_FOR_STOP.get(str(stop_reason), str(RunStatus.STOPPED))
        self.store.set_status(status, stop_reason)
        if final_answer is not None:
            state.final_answer = final_answer
        self.checkpoint.save(state)
        self._audit_state("RUN_FINISHED", reason,
                          {"stop_reason": str(stop_reason), "accepted": accepted, "status": status})
        return RunOutcome(
            run_id=self.run_id, status=status, stop_reason=str(stop_reason), accepted=accepted,
            reason=reason, final_answer=final_answer, steps_used=state.budget.steps_used,
            writes_used=state.budget.writes_used, resumed=self._recovery is not None,
            recovery=self._recovery.to_dict() if self._recovery is not None else None,
        )

    # ------------------------------------------------------------------ #
    # 交付：完整运行记录
    # ------------------------------------------------------------------ #
    def export_run_record(self) -> dict[str, Any]:
        """导出一次运行的完整可审计记录（运行/预算/证据/步骤/审计链/恢复对账）。"""

        state = self.store.state
        return {
            "run": {
                "run_id": state.run_id,
                "task": state.task,
                "task_kind": str(state.task_kind),
                "status": str(state.status),
                "stop_reason": str(state.stop_reason) if state.stop_reason is not None else None,
                "final_answer": state.final_answer,
                "workspace_fingerprint": state.workspace_fingerprint,
            },
            "budget": state.budget.to_dict(),
            "evidence": [e.to_dict() for e in state.evidence],
            "steps": [s.to_dict() for s in state.steps],
            "approvals": [a.to_dict() for a in state.approvals],
            "audit_chain": [r.to_dict() for r in self.audit.records],
            "recovery": self._recovery.to_dict() if self._recovery is not None else None,
        }
