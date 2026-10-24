"""tests/test_completion.py —— 最终验收判定（H1 交付物/证据齐备 / H2 证据新鲜度 / H3 判定确定性）。"""

from __future__ import annotations

from completion import (
    DEFAULT_TASK_SPECS,
    EMPTY_ANSWER,
    NO_FRESH_DELIVERABLE,
    NO_GROUNDED_FILE_READ,
    NO_PASSING_TEST_ON_CURRENT_VERSION,
    TEST_EVIDENCE_STALE_RERUN_REQUIRED,
    CompletionChecker,
    CompletionVerdict,
)
from state_store import Evidence, EvidenceKind, RunState, StopReason, TaskKind

FP = "FP-CURRENT"


def _state(kind: TaskKind) -> RunState:
    return RunState(run_id="r", task="t", task_kind=kind, workspace_root="/ws")


def _ev(kind: EvidenceKind, fingerprint: str, *, passed=None, ref="a.py", invalidated=False) -> Evidence:
    detail = {} if passed is None else {"passed": passed}
    return Evidence(kind=kind, ref=ref, detail=detail, fingerprint=fingerprint,
                    step_number=1, invalidated=invalidated)


# --------------------------------------------------------------------------- #
# 默认规格
# --------------------------------------------------------------------------- #
def test_default_specs_present():
    assert TaskKind.CODE_UNDERSTANDING in DEFAULT_TASK_SPECS
    assert TaskKind.TEST_REPAIR in DEFAULT_TASK_SPECS
    assert DEFAULT_TASK_SPECS[TaskKind.TEST_REPAIR].requires_passing_tests is True


# --------------------------------------------------------------------------- #
# H1：答案非空
# --------------------------------------------------------------------------- #
def test_empty_answer_rejected(completion):
    v = completion.evaluate("   ", _state(TaskKind.CODE_UNDERSTANDING), FP)
    assert v.accepted is False
    assert EMPTY_ANSWER in v.missing


def test_none_answer_rejected(completion):
    v = completion.evaluate(None, _state(TaskKind.CODE_UNDERSTANDING), FP)
    assert EMPTY_ANSWER in v.missing


# --------------------------------------------------------------------------- #
# H1：代码理解需要“当前版本上仍有效”的真实阅读
# --------------------------------------------------------------------------- #
def test_code_understanding_needs_grounded_read(completion):
    v = completion.evaluate("答案", _state(TaskKind.CODE_UNDERSTANDING), FP)
    assert v.accepted is False
    assert NO_GROUNDED_FILE_READ in v.missing


def test_code_understanding_accepts_with_fresh_read(completion):
    st = _state(TaskKind.CODE_UNDERSTANDING)
    st.evidence.append(_ev(EvidenceKind.FILE_READ, FP))
    v = completion.evaluate("答案", st, FP)
    assert v.accepted is True
    assert v.stop_reason == StopReason.COMPLETED


def test_code_understanding_rejects_stale_read(completion):
    st = _state(TaskKind.CODE_UNDERSTANDING)
    st.evidence.append(_ev(EvidenceKind.FILE_READ, "FP-OLD"))  # 停留在旧版本
    v = completion.evaluate("答案", st, FP)
    assert v.accepted is False
    assert NO_GROUNDED_FILE_READ in v.missing


def test_code_understanding_rejects_invalidated_read(completion):
    st = _state(TaskKind.CODE_UNDERSTANDING)
    st.evidence.append(_ev(EvidenceKind.FILE_READ, FP, invalidated=True))
    v = completion.evaluate("答案", st, FP)
    assert NO_GROUNDED_FILE_READ in v.missing


# --------------------------------------------------------------------------- #
# H1/H2：测试修复需要新鲜交付物 + 当前版本通过的测试
# --------------------------------------------------------------------------- #
def test_test_repair_needs_deliverable_and_passing(completion):
    v = completion.evaluate("修好了", _state(TaskKind.TEST_REPAIR), FP)
    assert v.accepted is False
    assert NO_FRESH_DELIVERABLE in v.missing
    assert NO_PASSING_TEST_ON_CURRENT_VERSION in v.missing


def test_test_repair_accepts_fresh_deliverable_and_passing_test(completion):
    st = _state(TaskKind.TEST_REPAIR)
    st.evidence.append(_ev(EvidenceKind.DELIVERABLE, FP))
    st.evidence.append(_ev(EvidenceKind.TEST_RESULT, FP, passed=True, ref="pytest"))
    v = completion.evaluate("修好了", st, FP)
    assert v.accepted is True
    assert v.stop_reason == StopReason.COMPLETED


def test_test_repair_rejects_failing_test_on_current_version(completion):
    st = _state(TaskKind.TEST_REPAIR)
    st.evidence.append(_ev(EvidenceKind.DELIVERABLE, FP))
    st.evidence.append(_ev(EvidenceKind.TEST_RESULT, FP, passed=False, ref="pytest"))
    v = completion.evaluate("修好了", st, FP)
    assert v.accepted is False
    assert NO_PASSING_TEST_ON_CURRENT_VERSION in v.missing


def test_test_repair_stale_passing_test_requires_rerun(completion):
    # H2：测试证据停留在旧版本指纹（源码已改）→ 判为失效，必须在当前版本重跑。
    st = _state(TaskKind.TEST_REPAIR)
    st.evidence.append(_ev(EvidenceKind.DELIVERABLE, FP))
    st.evidence.append(_ev(EvidenceKind.TEST_RESULT, "FP-OLD", passed=True, ref="pytest"))
    v = completion.evaluate("修好了", st, FP)
    assert v.accepted is False
    assert TEST_EVIDENCE_STALE_RERUN_REQUIRED in v.missing


def test_test_repair_stale_deliverable_rejected(completion):
    st = _state(TaskKind.TEST_REPAIR)
    st.evidence.append(_ev(EvidenceKind.DELIVERABLE, "FP-OLD"))
    st.evidence.append(_ev(EvidenceKind.TEST_RESULT, FP, passed=True, ref="pytest"))
    v = completion.evaluate("修好了", st, FP)
    assert v.accepted is False
    assert NO_FRESH_DELIVERABLE in v.missing


# --------------------------------------------------------------------------- #
# 判定确定性 / 可序列化
# --------------------------------------------------------------------------- #
def test_verdict_to_dict(completion):
    st = _state(TaskKind.CODE_UNDERSTANDING)
    st.evidence.append(_ev(EvidenceKind.FILE_READ, FP))
    v = completion.evaluate("答案", st, FP)
    assert isinstance(v, CompletionVerdict)
    d = v.to_dict()
    assert d["accepted"] is True and d["missing"] == [] and d["stop_reason"] == str(StopReason.COMPLETED)


def test_evaluate_is_side_effect_free(completion):
    st = _state(TaskKind.CODE_UNDERSTANDING)
    st.evidence.append(_ev(EvidenceKind.FILE_READ, FP))
    before = len(st.evidence)
    completion.evaluate("答案", st, FP)
    completion.evaluate("答案", st, FP)
    assert len(st.evidence) == before  # 判定不修改状态


def test_custom_spec_override():
    from completion import TaskSpec

    checker = CompletionChecker(specs={TaskKind.CODE_UNDERSTANDING: TaskSpec()})
    # 关闭 grounded-read 要求后，空证据也可接受（只要答案非空）。
    v = checker.evaluate("答案", _state(TaskKind.CODE_UNDERSTANDING), FP)
    assert v.accepted is True
