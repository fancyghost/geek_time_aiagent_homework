"""gate.py —— 所有 Tool Call 的**唯一入口**与门禁（G1–G4）。

一次 ``execute(GateRequest)`` 依次完成：
1. 工具存在性；
2. **G1** 参数校验（pydantic 严格模型，缺字段/类型错误/多余字段一律拒绝，handler 执行 0 次）；
3. 沙箱前置（S1 路径逃逸 / S2 软链接·敏感目录 / S3 命令白名单）；
4. **G2** 连续重复动作拦截（同一 (工具,参数) 连续达到阈值即拒绝）；
5. **G4** 幂等（相同写动作已成功执行过 → 拒绝重放）；
6. 写预算；
7. **G3** 审批（写操作需授权：无授权则登记待审批并置 WAITING_APPROVAL、停止等待）；
8. 放行 → 写意图先行登记（C2）→ 调用 handler → 登记证据/步骤/预算 → 审计结果。

**被拒动作绝不进入 handler**，一律返回结构化 ``GateResult(ok=False, ...)`` 并保留原 ``tool_call_id``，
同时写入 append-only 审计（A1）。业务规则集中在此，handler 保持“哑”。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from audit import AuditActor, AuditDecision, AuditLog, AuditPhase
from catalog import Effect, HandlerDeps, ToolSpec
from sandbox import Sandbox, SandboxViolation
from state_store import (
    ResultStatus,
    RunStatus,
    StateStore,
    StepRecord,
    args_digest,
)


# --------------------------------------------------------------------------- #
# 输入 / 上下文 / 结果
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class GateRequest:
    """一次工具调用请求（``tool_call_id`` 由系统注入，模型不可见）。"""

    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class GateContext:
    """当前运行的门禁上下文：run、步号与动作发起者。"""

    run_id: str
    step_number: int = 0
    actor: str = AuditActor.MODEL


@dataclass(slots=True)
class GateResult:
    """门禁裁决 + 执行结果（``to_dict()`` 会作为 observation 喂回模型）。"""

    ok: bool
    tool_name: str
    tool_call_id: str
    code: str = ""
    message: str = ""
    decision: str = ""
    output: Any = None
    is_final_answer: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "decision": self.decision,
            "code": self.code,
            "message": self.message,
            "content": self.output,
            "is_final_answer": self.is_final_answer,
        }


def _summarize_validation(exc: ValidationError) -> str:
    try:
        errors = exc.errors()
    except Exception:  # noqa: BLE001
        return str(exc)
    parts: list[str] = []
    for err in errors[:3]:
        loc = ".".join(str(x) for x in err.get("loc", ()))
        parts.append(f"{loc or '<root>'}: {err.get('msg', '')}")
    return "; ".join(parts) or str(exc)


# --------------------------------------------------------------------------- #
# Gate
# --------------------------------------------------------------------------- #
class Gate:
    """唯一入口：把“模型想做什么”与“程序允许做什么”对齐。"""

    def __init__(self, specs: dict[str, ToolSpec], sandbox: Sandbox, store: StateStore,
                 audit: AuditLog, *, duplicate_threshold: int = 3,
                 durable_save: Any | None = None) -> None:
        self.specs = specs
        self.sandbox = sandbox
        self.store = store
        self.audit = audit
        self.duplicate_threshold = duplicate_threshold
        self._durable_save = durable_save
        self.context = GateContext(run_id=store.run_id, step_number=0)

    @property
    def state(self):
        return self.store.state

    def set_context(self, context: GateContext) -> None:
        self.context = context

    # ---- 内部工具 ----
    def _durable(self) -> None:
        if self._durable_save is not None:
            try:
                self._durable_save()
            except Exception:  # noqa: BLE001 - 持久化失败不阻断门禁裁决
                pass

    def _audit(self, phase, decision, rule, *, actor=None, tool_call_id=None,
               tool_name=None, message="", detail=None, latency_ms=None) -> None:
        self.audit.append(
            run_id=self.context.run_id or self.store.run_id, phase=phase, decision=decision,
            rule=rule, actor=actor or self.context.actor, tool_call_id=tool_call_id,
            tool_name=tool_name, step_number=self.context.step_number, message=message,
            detail=detail, latency_ms=latency_ms,
        )

    def _record(self, tool_call_id, tool_name, arguments, digest, *, decision, rule,
                result_status, is_write=False, idempotency_key="", observation=None) -> StepRecord:
        return self.store.add_step(StepRecord(
            step_number=self.context.step_number, tool_call_id=tool_call_id, tool_name=tool_name,
            args_digest=digest, decision=decision, rule=rule, result_status=result_status,
            is_write=is_write, idempotency_key=idempotency_key, arguments=dict(arguments),
            observation=observation, ended_at=time.time(),
        ))

    def _sandbox_precheck(self, spec: ToolSpec, args: Any) -> None:
        is_write = spec.policy.effect == Effect.WRITE
        for field_name in spec.path_fields:
            value = getattr(args, field_name, None)
            if value is None:
                continue
            self.sandbox.resolve_path(str(value), for_write=is_write, must_exist=not is_write)
        if spec.command_field:
            value = getattr(args, spec.command_field, None)
            if value is not None:
                self.sandbox.assert_command_allowed(str(value))

    # ---- 唯一入口 ----
    def execute(self, request: GateRequest) -> GateResult:
        state = self.store.state
        tool_call_id = request.tool_call_id
        tool_name = request.tool_name
        arguments = dict(request.arguments or {})

        spec = self.specs.get(tool_name)
        if spec is None:
            self._audit(AuditPhase.GATE, AuditDecision.DENY, "TOOL_NOT_FOUND",
                        tool_call_id=tool_call_id, tool_name=tool_name, message=f"未知工具：{tool_name!r}")
            return GateResult(False, tool_name, tool_call_id, code="TOOL_NOT_FOUND",
                              decision="deny", message=f"未知工具：{tool_name!r}")

        # G1：参数校验（严格模型）——失败则 handler 执行 0 次。
        try:
            args = spec.args_model(**arguments)
        except ValidationError as exc:
            digest = args_digest(tool_name, arguments)
            self._record(tool_call_id, tool_name, arguments, digest, decision="deny",
                         rule="INVALID_ARGUMENT", result_status=ResultStatus.DENIED)
            self._audit(AuditPhase.GATE, AuditDecision.DENY, "INVALID_ARGUMENT",
                        tool_call_id=tool_call_id, tool_name=tool_name, message="参数校验失败")
            return GateResult(False, tool_name, tool_call_id, code="INVALID_ARGUMENT", decision="deny",
                              message=f"参数校验失败：{_summarize_validation(exc)}")

        # 沙箱前置（S1/S2/S3）。
        try:
            self._sandbox_precheck(spec, args)
        except SandboxViolation as exc:
            digest = args_digest(tool_name, arguments)
            self._record(tool_call_id, tool_name, arguments, digest, decision="deny",
                         rule=exc.code, result_status=ResultStatus.DENIED)
            self._audit(AuditPhase.SANDBOX, AuditDecision.DENY, exc.code, actor=AuditActor.HARNESS,
                        tool_call_id=tool_call_id, tool_name=tool_name, message=exc.message)
            return GateResult(False, tool_name, tool_call_id, code=exc.code, decision="deny", message=exc.message)

        digest = args_digest(tool_name, arguments)
        is_write = spec.policy.effect == Effect.WRITE

        # G2：连续重复动作。
        count = self.store.register_request(digest)
        if count >= self.duplicate_threshold:
            self._record(tool_call_id, tool_name, arguments, digest, decision="deny",
                         rule="DUPLICATE_ACTION", result_status=ResultStatus.DENIED)
            self._audit(AuditPhase.GATE, AuditDecision.DENY, "DUPLICATE_ACTION",
                        tool_call_id=tool_call_id, tool_name=tool_name,
                        message=f"连续 {count} 次相同动作，已拦截")
            return GateResult(False, tool_name, tool_call_id, code="DUPLICATE_ACTION", decision="deny",
                              message=f"连续 {count} 次重复相同动作，已拦截；请改变策略或收敛。")

        # G4：幂等——相同写动作已成功执行过则拒绝重放。
        if is_write and self.store.has_executed(digest):
            self._record(tool_call_id, tool_name, arguments, digest, decision="deny",
                         rule="ALREADY_EXECUTED", result_status=ResultStatus.DENIED,
                         is_write=True, idempotency_key=digest)
            self._audit(AuditPhase.GATE, AuditDecision.DENY, "ALREADY_EXECUTED",
                        tool_call_id=tool_call_id, tool_name=tool_name, message="该写动作已执行过（幂等拒绝）")
            return GateResult(False, tool_name, tool_call_id, code="ALREADY_EXECUTED", decision="deny",
                              message="相同的写操作此前已成功执行，幂等拒绝重复执行。")

        # 写预算。
        if is_write and state.budget.writes_exhausted():
            self._record(tool_call_id, tool_name, arguments, digest, decision="deny",
                         rule="WRITE_BUDGET_EXCEEDED", result_status=ResultStatus.DENIED, is_write=True)
            self._audit(AuditPhase.GATE, AuditDecision.DENY, "WRITE_BUDGET_EXCEEDED",
                        tool_call_id=tool_call_id, tool_name=tool_name, message="写入次数预算已用尽")
            return GateResult(False, tool_name, tool_call_id, code="WRITE_BUDGET_EXCEEDED", decision="deny",
                              message="写入次数预算已用尽，拒绝新的写操作。")

        # G3：审批。
        consumed_approval = None
        if spec.policy.requires_approval:
            approval = self.store.find_approval(tool_name, digest, active_only=True)
            if approval is not None:
                self.store.consume_approval(approval.approval_id)
                consumed_approval = approval
                self._clear_matching_pending(digest)
                if str(state.status) == str(RunStatus.WAITING_APPROVAL):
                    self.store.set_status(RunStatus.RUNNING, None)
            elif str(self.context.actor) == str(AuditActor.USER):
                self._record(tool_call_id, tool_name, arguments, digest, decision="deny",
                             rule="NO_APPROVAL", result_status=ResultStatus.DENIED, is_write=True)
                self._audit(AuditPhase.APPROVAL, AuditDecision.DENY, "NO_APPROVAL",
                            tool_call_id=tool_call_id, tool_name=tool_name, message="缺少有效审批")
                return GateResult(False, tool_name, tool_call_id, code="NO_APPROVAL", decision="deny",
                                  message="该写操作缺少有效审批，已拒绝。")
            else:
                # 模型请求写但未获批 → 登记待审批、置 WAITING_APPROVAL、停止等待人工。
                self.store.set_pending_action(tool_name, digest, tool_call_id, arguments=arguments)
                self.store.set_status(RunStatus.WAITING_APPROVAL, None)
                self._record(tool_call_id, tool_name, arguments, digest, decision="confirm",
                             rule="WAITING_APPROVAL", result_status=ResultStatus.DENIED, is_write=True)
                self._audit(AuditPhase.APPROVAL, AuditDecision.CONFIRM, "WAITING_APPROVAL",
                            tool_call_id=tool_call_id, tool_name=tool_name,
                            message=f"写操作等待人工审批：{spec.canonical_target(args)}",
                            detail={"args_digest": digest})
                self._durable()
                return GateResult(False, tool_name, tool_call_id, code="WAITING_APPROVAL", decision="confirm",
                                  message="写操作已登记，等待人工审批后执行。")

        # 放行。
        self._audit(AuditPhase.GATE, AuditDecision.ALLOW, "ALLOWED",
                    tool_call_id=tool_call_id, tool_name=tool_name, message="全部前置检查通过")
        if consumed_approval is not None:
            self._audit(AuditPhase.APPROVAL, AuditDecision.APPROVED, "APPROVED",
                        actor=consumed_approval.actor, tool_call_id=tool_call_id, tool_name=tool_name,
                        message=f"审批 {consumed_approval.approval_id} 已消费", detail={"args_digest": digest})

        # C2：写意图先行登记并落盘——若随后崩溃，恢复时对账为 UNKNOWN_RESULT，禁止自动重放。
        pending_record: StepRecord | None = None
        if is_write:
            pending_record = self.store.add_step(StepRecord(
                step_number=self.context.step_number, tool_call_id=tool_call_id, tool_name=tool_name,
                args_digest=digest, decision="allow", rule="ALLOWED", result_status=ResultStatus.PENDING,
                is_write=True, idempotency_key=digest, arguments=arguments,
            ))
            self._durable()

        deps = HandlerDeps(
            sandbox=self.sandbox, state=self.store, run_id=self.context.run_id or state.run_id,
            step_number=self.context.step_number, tool_call_id=tool_call_id,
            fingerprint=self.sandbox.workspace_fingerprint, timeout_seconds=spec.policy.timeout_seconds,
        )
        started = time.perf_counter()
        try:
            outcome = spec.handler(args, deps)
        except SandboxViolation as exc:
            return self._fail(tool_call_id, tool_name, arguments, digest, exc.code, exc.message,
                              pending_record, is_write)
        except Exception as exc:  # noqa: BLE001 - handler 异常转为结构化失败
            return self._fail(tool_call_id, tool_name, arguments, digest, "HANDLER_ERROR",
                              f"{type(exc).__name__}: {exc}", pending_record, is_write)

        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        observation = json.dumps({"content": outcome.result}, ensure_ascii=False)[:4000]

        if pending_record is not None:
            pending_record.result_status = ResultStatus.SUCCESS
            pending_record.observation = observation
            pending_record.ended_at = time.time()
        else:
            self._record(tool_call_id, tool_name, arguments, digest, decision="allow", rule="ALLOWED",
                         result_status=ResultStatus.SUCCESS, is_write=bool(outcome.is_write),
                         idempotency_key=digest if outcome.is_write else "", observation=observation)

        if outcome.evidence is not None:
            self.store.add_evidence(outcome.evidence)
        if outcome.is_write:
            self.store.record_write()
        self._clear_matching_pending(digest)
        if str(state.status) == str(RunStatus.WAITING_APPROVAL):
            self.store.set_status(RunStatus.RUNNING, None)

        self._audit(AuditPhase.RESULT, AuditDecision.EXECUTED, "OK", actor=AuditActor.HARNESS,
                    tool_call_id=tool_call_id, tool_name=tool_name, message="handler executed",
                    latency_ms=latency_ms, detail={"is_write": bool(outcome.is_write)})
        self._durable()
        return GateResult(True, tool_name, tool_call_id, code="OK", decision="allow", message="执行成功",
                          output=outcome.result, is_final_answer=(tool_name == "final_answer"))

    # ---- 失败/清理辅助 ----
    def _fail(self, tool_call_id, tool_name, arguments, digest, code, message,
              pending_record: StepRecord | None, is_write: bool) -> GateResult:
        if pending_record is not None:
            pending_record.result_status = ResultStatus.FAILED
            pending_record.observation = message
            pending_record.ended_at = time.time()
        else:
            self._record(tool_call_id, tool_name, arguments, digest, decision="deny", rule=code,
                         result_status=ResultStatus.FAILED, is_write=is_write, observation=message)
        self._audit(AuditPhase.EXECUTION, AuditDecision.FAILED, code, actor=AuditActor.HARNESS,
                    tool_call_id=tool_call_id, tool_name=tool_name, message=message)
        self._durable()
        return GateResult(False, tool_name, tool_call_id, code=code, decision="deny", message=message)

    def _clear_matching_pending(self, digest: str) -> None:
        pending = self.store.state.pending_action
        if pending and pending.get("args_digest") == digest:
            self.store.clear_pending_action()
