"""completion.py —— 最终验收（H1 / H2 / H3 判定层）。

模型调用 ``final_answer`` 只是“声称完成”；是否**真的可被接受**由本模块判定：
- H1：答案为空 / 缺真实阅读（代码理解）/ 缺新鲜交付物（测试修复）→ 拒绝；
- H2：测试证据停留在旧版本指纹（源码已改）→ 判为失效，必须在当前版本重跑；
- H3：预算/最大轮数属于主循环职责（见 harness.py），此处只判“交付物与证据是否达标”。

判定是**确定性、无副作用**的：只读 ``RunState`` 的证据与当前工作区指纹。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from state_store import EvidenceKind, RunState, StopReason, TaskKind

# 未达标原因码（会喂回模型，指导其补齐）。
EMPTY_ANSWER = "EMPTY_ANSWER"
NO_GROUNDED_FILE_READ = "NO_GROUNDED_FILE_READ"
NO_FRESH_DELIVERABLE = "NO_FRESH_DELIVERABLE"
NO_PASSING_TEST_ON_CURRENT_VERSION = "NO_PASSING_TEST_ON_CURRENT_VERSION"
TEST_EVIDENCE_STALE_RERUN_REQUIRED = "TEST_EVIDENCE_STALE_RERUN_REQUIRED"


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """某类任务“可被接受为完成”的硬性要求。"""

    requires_nonempty_answer: bool = True
    requires_grounded_reads: bool = False
    requires_deliverable: bool = False
    requires_passing_tests: bool = False


# 两类仓库任务的默认验收规格。
DEFAULT_TASK_SPECS: dict[TaskKind, TaskSpec] = {
    TaskKind.CODE_UNDERSTANDING: TaskSpec(requires_grounded_reads=True),
    TaskKind.TEST_REPAIR: TaskSpec(requires_deliverable=True, requires_passing_tests=True),
}


@dataclass(slots=True)
class CompletionVerdict:
    """一次完成判定结论。"""

    accepted: bool
    missing: list[str] = field(default_factory=list)
    reason: str = ""
    stop_reason: StopReason | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "missing": list(self.missing),
            "reason": self.reason,
            "stop_reason": str(self.stop_reason) if self.stop_reason is not None else None,
        }


class CompletionChecker:
    """把“声称完成”与“真实证据”对齐的验收器。"""

    def __init__(self, specs: dict[TaskKind, TaskSpec] | None = None) -> None:
        self.specs = specs or DEFAULT_TASK_SPECS

    def _spec_for(self, state: RunState) -> TaskSpec:
        try:
            kind = TaskKind(state.task_kind)
        except ValueError:
            kind = TaskKind.CODE_UNDERSTANDING
        return self.specs.get(kind, TaskSpec())

    def evaluate(self, answer: str | None, state: RunState, fingerprint: str) -> CompletionVerdict:
        spec = self._spec_for(state)
        missing: list[str] = []
        evidence = state.evidence

        # H1：答案不能为空。
        text = (answer or "").strip()
        if spec.requires_nonempty_answer and not text:
            missing.append(EMPTY_ANSWER)

        # H1（代码理解）：必须有“当前版本上仍然有效”的真实文件阅读。
        if spec.requires_grounded_reads:
            fresh_reads = [
                e for e in evidence
                if str(e.kind) == str(EvidenceKind.FILE_READ) and e.is_fresh(fingerprint)
            ]
            if not fresh_reads:
                missing.append(NO_GROUNDED_FILE_READ)

        # H1（测试修复）：必须有当前版本上的新鲜交付物。
        if spec.requires_deliverable:
            fresh_deliverables = [
                e for e in evidence
                if str(e.kind) == str(EvidenceKind.DELIVERABLE) and e.is_fresh(fingerprint)
            ]
            if not fresh_deliverables:
                missing.append(NO_FRESH_DELIVERABLE)

        # H2（测试修复）：必须有“当前版本上通过”的测试证据。
        if spec.requires_passing_tests:
            tests = [e for e in evidence if str(e.kind) == str(EvidenceKind.TEST_RESULT)]
            passed_current = [
                e for e in tests
                if e.is_fresh(fingerprint) and e.detail.get("passed") is True
            ]
            if not passed_current:
                # 区分“证据停留在旧版本”（须重跑）与“当前版本就没有通过的测试”。
                stale_passed = [
                    e for e in tests
                    if (not e.invalidated) and (not e.is_fresh(fingerprint))
                    and e.detail.get("passed") is True
                ]
                if stale_passed:
                    missing.append(TEST_EVIDENCE_STALE_RERUN_REQUIRED)
                else:
                    missing.append(NO_PASSING_TEST_ON_CURRENT_VERSION)

        if not missing:
            return CompletionVerdict(
                accepted=True, missing=[], reason="交付物与证据齐备且在当前版本有效",
                stop_reason=StopReason.COMPLETED,
            )
        return CompletionVerdict(
            accepted=False, missing=missing,
            reason="完成检查未通过：" + ", ".join(missing), stop_reason=None,
        )
