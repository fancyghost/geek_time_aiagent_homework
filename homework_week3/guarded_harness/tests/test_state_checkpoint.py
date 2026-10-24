"""tests/test_state_checkpoint.py —— 状态存储与 Checkpoint（C1 原子保存 / C2 结果未知不重放 / C3 计数不清零 / C4 工作区变化对账）。"""

from __future__ import annotations

import json

from checkpoint import Checkpoint
from state_store import (
    Approval,
    Budget,
    Evidence,
    EvidenceKind,
    ResultStatus,
    RunState,
    RunStatus,
    StateStore,
    StepRecord,
    StopReason,
    TaskKind,
    args_digest,
)


# --------------------------------------------------------------------------- #
# args_digest：内容寻址、稳定、顺序无关
# --------------------------------------------------------------------------- #
def test_args_digest_stable_and_order_independent():
    d1 = args_digest("write_file", {"path": "a.py", "content": "x"})
    d2 = args_digest("write_file", {"content": "x", "path": "a.py"})
    assert d1 == d2
    assert d1 == args_digest("write_file", {"path": "a.py", "content": "x"})


def test_args_digest_differs_on_change():
    assert args_digest("write_file", {"path": "a.py", "content": "x"}) != args_digest(
        "write_file", {"path": "a.py", "content": "y"}
    )
    assert args_digest("write_file", {"path": "a.py"}) != args_digest("read_file", {"path": "a.py"})


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #
def test_budget_exhaustion_helpers():
    b = Budget(max_steps=2, max_writes=1, max_seconds=1000)
    assert not b.steps_exhausted()
    b.steps_used = 2
    assert b.steps_exhausted()
    b.writes_used = 1
    assert b.writes_exhausted()
    d = b.to_dict()
    assert d["steps_used"] == 2 and d["max_writes"] == 1


def test_budget_seconds_exhausted():
    b = Budget(max_seconds=0.0)
    assert b.seconds_exhausted()


# --------------------------------------------------------------------------- #
# StateStore：步骤 / 幂等 / 证据 / 审批 / 待审批 / 重复计数
# --------------------------------------------------------------------------- #
def test_store_consume_step_and_write(store):
    store.consume_step()
    store.consume_step()
    store.record_write()
    assert store.state.budget.steps_used == 2
    assert store.state.budget.writes_used == 1


def test_store_idempotency_has_executed(store):
    store.add_step(StepRecord(step_number=1, tool_call_id="t1", tool_name="write_file",
                              idempotency_key="k1", result_status=ResultStatus.SUCCESS, is_write=True))
    assert store.has_executed("k1") is True
    assert store.has_executed("k2") is False
    assert store.has_executed("") is False


def test_store_has_executed_ignores_non_success(store):
    store.add_step(StepRecord(step_number=1, tool_call_id="t1", tool_name="write_file",
                              idempotency_key="k1", result_status=ResultStatus.PENDING, is_write=True))
    assert store.has_executed("k1") is False


def test_store_register_request_counts_consecutive(store):
    assert store.register_request("a") == 1
    assert store.register_request("a") == 2
    assert store.register_request("a") == 3
    # 不同 digest 打断连续计数。
    assert store.register_request("b") == 1
    assert store.register_request("a") == 1


def test_store_evidence_and_invalidate(store):
    ev = Evidence(kind=EvidenceKind.FILE_READ, ref="a.py", detail={}, fingerprint="fp1", step_number=1)
    store.add_evidence(ev)
    assert store.evidence_of(EvidenceKind.FILE_READ) == [ev]
    assert ev.is_fresh("fp1") is True
    n = store.invalidate_all_evidence("workspace_changed")
    assert n == 1
    assert ev.invalidated is True
    assert ev.is_fresh("fp1") is False


def test_store_approval_lifecycle(store):
    ap = store.grant_approval("ap1", "write_file", "dig", actor="user")
    assert store.find_approval("write_file", "dig") is ap
    assert store.consume_approval("ap1") is True
    # 消费后 active_only 查不到，但非 active_only 仍可查到。
    assert store.find_approval("write_file", "dig", active_only=True) is None
    assert store.find_approval("write_file", "dig", active_only=False) is ap


def test_store_revoke_approvals_skips_consumed(store):
    store.grant_approval("ap1", "write_file", "d1")
    store.grant_approval("ap2", "write_file", "d2")
    store.consume_approval("ap1")
    revoked = store.revoke_approvals("workspace_changed")
    assert revoked == 1  # 只吊销未消费的 ap2
    assert store.find_approval("write_file", "d2") is None


def test_store_pending_action(store):
    store.set_pending_action("write_file", "dig", "t1", arguments={"path": "a.py"})
    assert store.state.pending_action["tool_name"] == "write_file"
    store.clear_pending_action()
    assert store.state.pending_action is None


# --------------------------------------------------------------------------- #
# RunState 序列化往返
# --------------------------------------------------------------------------- #
def test_runstate_roundtrip():
    st = RunState(run_id="r1", task="do", task_kind=TaskKind.TEST_REPAIR, workspace_root="/ws",
                  budget=Budget(max_steps=5, steps_used=2, writes_used=1))
    st.evidence.append(Evidence(kind=EvidenceKind.DELIVERABLE, ref="a.py", detail={"bytes": 3},
                                fingerprint="fp", step_number=1, tool_call_id="t1"))
    st.steps.append(StepRecord(step_number=1, tool_call_id="t1", tool_name="write_file",
                               args_digest="d", decision="allow", rule="ALLOWED",
                               result_status=ResultStatus.SUCCESS, is_write=True, idempotency_key="d"))
    st.approvals.append(Approval(approval_id="ap1", tool_name="write_file", args_digest="d"))
    st.status = RunStatus.WAITING_APPROVAL
    st.stop_reason = StopReason.WAITING_APPROVAL
    st.pending_action = {"tool_name": "write_file", "args_digest": "d", "tool_call_id": "t1", "arguments": {}}
    st.workspace_fingerprint = "fp"

    data = json.loads(json.dumps(st.to_dict(), ensure_ascii=False))
    back = RunState.from_dict(data)
    assert back.run_id == "r1"
    assert str(back.task_kind) == str(TaskKind.TEST_REPAIR)
    assert back.budget.steps_used == 2 and back.budget.writes_used == 1
    assert str(back.status) == str(RunStatus.WAITING_APPROVAL)
    assert str(back.stop_reason) == str(StopReason.WAITING_APPROVAL)
    assert back.evidence[0].ref == "a.py"
    assert str(back.steps[0].result_status) == str(ResultStatus.SUCCESS)
    assert back.approvals[0].approval_id == "ap1"
    assert back.pending_action["tool_name"] == "write_file"
    assert back.workspace_fingerprint == "fp"


# --------------------------------------------------------------------------- #
# Checkpoint：C1 原子保存 / C3 计数不清零
# --------------------------------------------------------------------------- #
def test_checkpoint_save_load_roundtrip(checkpoint, state):
    state.budget.steps_used = 3
    state.budget.writes_used = 1
    state.status = RunStatus.WAITING_APPROVAL
    checkpoint.save(state)
    assert checkpoint.exists(state.run_id)
    loaded = checkpoint.load(state.run_id)
    assert loaded is not None
    # C3：预算计数原样恢复，不清零。
    assert loaded.budget.steps_used == 3
    assert loaded.budget.writes_used == 1
    assert str(loaded.status) == str(RunStatus.WAITING_APPROVAL)


def test_checkpoint_save_records_fingerprint(tmp_path, workspace):
    fp_holder = {"fp": "FP-1"}
    ck = Checkpoint(tmp_path / "ck", fingerprint_fn=lambda: fp_holder["fp"])
    st = RunState(run_id="r-fp", workspace_root=str(workspace))
    ck.save(st)
    assert st.workspace_fingerprint == "FP-1"
    loaded = ck.load("r-fp")
    assert loaded.workspace_fingerprint == "FP-1"


def test_checkpoint_load_latest(tmp_path, workspace):
    ck = Checkpoint(tmp_path / "ck")
    s1 = RunState(run_id="r1", workspace_root=str(workspace))
    s2 = RunState(run_id="r2", workspace_root=str(workspace))
    ck.save(s1)
    ck.save(s2)
    latest = ck.load_latest()
    assert latest is not None and latest.run_id == "r2"


def test_checkpoint_load_missing_returns_none(checkpoint):
    assert checkpoint.load("does-not-exist") is None


def test_checkpoint_atomic_no_temp_leftover(checkpoint, state):
    checkpoint.save(state)
    files = list(checkpoint.dir.glob("*"))
    names = {p.name for p in files}
    assert f"{state.run_id}.json" in names
    # 不应残留 .tmp 临时文件。
    assert not any(n.endswith(".tmp") for n in names)


# --------------------------------------------------------------------------- #
# reconcile：C2 结果未知 / C4 工作区变化
# --------------------------------------------------------------------------- #
def test_reconcile_flags_unknown_result_writes(tmp_path, workspace):
    ck = Checkpoint(tmp_path / "ck")
    st = RunState(run_id="r-c2", workspace_root=str(workspace))
    # 写意图已登记但结果未知（PENDING）——模拟“执行到一半崩溃”。
    st.steps.append(StepRecord(step_number=1, tool_call_id="tc-write", tool_name="write_file",
                               result_status=ResultStatus.PENDING, is_write=True, idempotency_key="k"))
    st.steps.append(StepRecord(step_number=2, tool_call_id="tc-read", tool_name="read_file",
                               result_status=ResultStatus.SUCCESS))
    report = ck.reconcile(st)
    assert report.unknown_result_tool_calls == ["tc-write"]
    assert any("unknown_result_writes" in n for n in report.notes)


def test_reconcile_workspace_change_invalidates_and_revokes(tmp_path, workspace):
    fp = {"v": "FP-OLD"}
    ck = Checkpoint(tmp_path / "ck", fingerprint_fn=lambda: fp["v"])
    st = RunState(run_id="r-c4", workspace_root=str(workspace))
    st.workspace_fingerprint = "FP-OLD"
    st.evidence.append(Evidence(kind=EvidenceKind.TEST_RESULT, ref="t", detail={"passed": True},
                                fingerprint="FP-OLD", step_number=1))
    st.approvals.append(Approval(approval_id="ap1", tool_name="write_file", args_digest="d"))
    # 工作区在恢复前发生变化。
    fp["v"] = "FP-NEW"
    report = ck.reconcile(st)
    assert report.workspace_changed is True
    assert report.invalidated_evidence == 1
    assert report.revoked_approvals == 1
    assert st.evidence[0].invalidated is True
    assert st.approvals[0].revoked is True
    assert st.workspace_fingerprint == "FP-NEW"


def test_reconcile_no_change_keeps_evidence(tmp_path, workspace):
    ck = Checkpoint(tmp_path / "ck", fingerprint_fn=lambda: "SAME")
    st = RunState(run_id="r-ok", workspace_root=str(workspace))
    st.workspace_fingerprint = "SAME"
    st.evidence.append(Evidence(kind=EvidenceKind.FILE_READ, ref="a.py", detail={},
                                fingerprint="SAME", step_number=1))
    report = ck.reconcile(st)
    assert report.workspace_changed is False
    assert report.invalidated_evidence == 0
    assert st.evidence[0].invalidated is False
