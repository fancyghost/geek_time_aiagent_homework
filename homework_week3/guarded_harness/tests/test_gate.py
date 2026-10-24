"""tests/test_gate.py —— 唯一入口门禁（G1 参数校验 / G2 连续重复 / G3 审批 / G4 幂等 + 写预算 + 沙箱前置）。"""

from __future__ import annotations

from audit import AuditActor
from gate import GateContext, GateRequest
from state_store import EvidenceKind, ResultStatus, RunStatus, args_digest


def _req(tool_call_id, tool_name, **arguments):
    return GateRequest(tool_call_id=tool_call_id, tool_name=tool_name, arguments=arguments)


# --------------------------------------------------------------------------- #
# 工具存在性
# --------------------------------------------------------------------------- #
def test_unknown_tool_denied(gate, audit):
    res = gate.execute(_req("nf", "nonexistent_tool"))
    assert res.ok is False
    assert res.code == "TOOL_NOT_FOUND"
    assert res.tool_call_id == "nf"
    chain = audit.chain("nf")
    assert chain and chain[0].rule == "TOOL_NOT_FOUND"


# --------------------------------------------------------------------------- #
# G1：严格参数校验（handler 执行 0 次）
# --------------------------------------------------------------------------- #
def test_g1_missing_required_arg_denied(gate, store, workspace, audit):
    # write_file 缺少 content → 拒绝，且不产生任何副作用/证据。
    res = gate.execute(_req("g1", "write_file", path="x.py"))
    assert res.ok is False and res.code == "INVALID_ARGUMENT"
    assert not (workspace / "x.py").exists()
    assert store.state.evidence == []
    chain = audit.chain("g1")
    assert chain[0].phase == "gate" and chain[0].decision == "deny"
    assert chain[0].rule == "INVALID_ARGUMENT" and chain[0].actor == str(AuditActor.MODEL)
    # 被拒步骤以 DENIED 落库。
    assert store.state.steps[-1].result_status == ResultStatus.DENIED


def test_g1_extra_arg_denied(gate):
    res = gate.execute(_req("g1x", "read_file", path="a.txt", unexpected="boom"))
    assert res.ok is False and res.code == "INVALID_ARGUMENT"


def test_g1_wrong_type_denied(gate):
    res = gate.execute(_req("g1t", "read_file", path=123))
    assert res.ok is False and res.code == "INVALID_ARGUMENT"


# --------------------------------------------------------------------------- #
# 沙箱前置（S1/S3）：在 handler 之前拦截
# --------------------------------------------------------------------------- #
def test_sandbox_precheck_path_escape(gate, audit):
    res = gate.execute(_req("sp", "read_file", path="../escape.txt"))
    assert res.ok is False and res.code == "PATH_ESCAPE"
    chain = audit.chain("sp")
    assert chain[0].phase == "sandbox" and chain[0].decision == "deny"
    assert chain[0].actor == str(AuditActor.HARNESS)


def test_sandbox_precheck_command_not_allowed(gate):
    res = gate.execute(_req("sc", "run_test", command="echo hello"))
    assert res.ok is False and res.code == "COMMAND_NOT_ALLOWED"


def test_sandbox_precheck_dangerous_command(gate):
    res = gate.execute(_req("sd", "run_test", command="python -c \"import os; os.system('rm -rf /')\""))
    assert res.ok is False and res.code == "DANGEROUS_COMMAND"


# --------------------------------------------------------------------------- #
# 只读放行 + 证据登记
# --------------------------------------------------------------------------- #
def test_read_file_allowed_and_records_evidence(gate, store, workspace, audit):
    (workspace / "note.txt").write_text("hello", encoding="utf-8")
    res = gate.execute(_req("r1", "read_file", path="note.txt"))
    assert res.ok is True
    assert res.output["chars"] == 5
    assert len(store.evidence_of(EvidenceKind.FILE_READ)) == 1
    rules = [r.rule for r in audit.chain("r1")]
    assert "ALLOWED" in rules


def test_final_answer_flagged(gate):
    res = gate.execute(_req("f1", "final_answer", answer="done"))
    assert res.ok is True and res.is_final_answer is True
    assert res.output == {"answer": "done"}


# --------------------------------------------------------------------------- #
# G2：连续重复动作
# --------------------------------------------------------------------------- #
def test_g2_duplicate_action_blocked_on_threshold(gate, workspace):
    (workspace / "readme.txt").write_text("hi", encoding="utf-8")
    r1 = gate.execute(_req("d1", "read_file", path="readme.txt"))
    r2 = gate.execute(_req("d2", "read_file", path="readme.txt"))
    r3 = gate.execute(_req("d3", "read_file", path="readme.txt"))
    assert r1.ok and r2.ok
    assert r3.ok is False and r3.code == "DUPLICATE_ACTION"


def test_g2_different_action_resets_counter(gate, workspace):
    (workspace / "a.txt").write_text("a", encoding="utf-8")
    (workspace / "b.txt").write_text("b", encoding="utf-8")
    assert gate.execute(_req("x1", "read_file", path="a.txt")).ok
    assert gate.execute(_req("x2", "read_file", path="a.txt")).ok
    assert gate.execute(_req("x3", "read_file", path="b.txt")).ok  # 打断连续
    assert gate.execute(_req("x4", "read_file", path="a.txt")).ok  # 重新计数，未达阈值


# --------------------------------------------------------------------------- #
# G3：审批
# --------------------------------------------------------------------------- #
def test_g3_write_without_approval_pauses(gate, store, workspace, audit):
    res = gate.execute(_req("w1", "write_file", path="o.py", content="y"))
    assert res.ok is False and res.code == "WAITING_APPROVAL"
    assert res.decision == "confirm"
    assert str(store.state.status) == str(RunStatus.WAITING_APPROVAL)
    assert store.state.pending_action["tool_name"] == "write_file"
    assert not (workspace / "o.py").exists()  # handler 未执行
    phases = [(r.phase, r.decision) for r in audit.chain("w1")]
    assert ("approval", "confirm") in phases


def test_g3_write_with_approval_executes(gate, store, workspace, audit):
    args = {"path": "out.py", "content": "x"}
    digest = args_digest("write_file", args)
    store.grant_approval("ap1", "write_file", digest, actor="user")
    res = gate.execute(_req("w2", "write_file", **args))
    assert res.ok is True
    assert (workspace / "out.py").read_text(encoding="utf-8") == "x"
    assert store.state.budget.writes_used == 1
    assert store.state.pending_action is None
    phases = [(r.phase, r.decision) for r in audit.chain("w2")]
    assert ("gate", "allow") in phases
    assert ("approval", "approved") in phases
    assert ("result", "executed") in phases
    # 审批一次性消费。
    assert store.find_approval("write_file", digest, active_only=True) is None


def test_g3_user_actor_without_approval_denied(gate):
    gate.set_context(GateContext(run_id="test-run", step_number=1, actor=AuditActor.USER))
    res = gate.execute(_req("w3", "write_file", path="o2.py", content="z"))
    assert res.ok is False and res.code == "NO_APPROVAL"


def test_g3_approval_resets_waiting_status(gate, store, workspace):
    # 先让模型请求写 → WAITING_APPROVAL。
    args = {"path": "p.py", "content": "1"}
    digest = args_digest("write_file", args)
    gate.execute(_req("p1", "write_file", **args))
    assert str(store.state.status) == str(RunStatus.WAITING_APPROVAL)
    # 授权后重放（模拟 approve_pending）→ 状态恢复 RUNNING。
    store.grant_approval("ap", "write_file", digest, actor="user")
    gate.set_context(GateContext(run_id="test-run", step_number=2, actor=AuditActor.USER))
    res = gate.execute(_req("p1", "write_file", **args))
    assert res.ok is True
    assert str(store.state.status) == str(RunStatus.RUNNING)


# --------------------------------------------------------------------------- #
# G4：幂等（相同写动作不重复执行）
# --------------------------------------------------------------------------- #
def test_g4_idempotent_write_rejected(gate, store, workspace):
    args = {"path": "g4.py", "content": "a"}
    digest = args_digest("write_file", args)
    store.grant_approval("a1", "write_file", digest)
    r1 = gate.execute(_req("c1", "write_file", **args))
    assert r1.ok is True
    # 再次授予审批并重放同一写动作 → G4 在 G3 之前拦截。
    store.grant_approval("a2", "write_file", digest)
    r2 = gate.execute(_req("c2", "write_file", **args))
    assert r2.ok is False and r2.code == "ALREADY_EXECUTED"


# --------------------------------------------------------------------------- #
# 写预算
# --------------------------------------------------------------------------- #
def test_write_budget_exceeded(gate, store):
    store.state.budget.max_writes = 1
    a1 = {"path": "b1.py", "content": "1"}
    store.grant_approval("ba1", "write_file", args_digest("write_file", a1))
    r1 = gate.execute(_req("bw1", "write_file", **a1))
    assert r1.ok is True and store.state.budget.writes_used == 1
    # 不同的写动作 → 预算耗尽（在审批之前拦截）。
    r2 = gate.execute(_req("bw2", "write_file", path="b2.py", content="2"))
    assert r2.ok is False and r2.code == "WRITE_BUDGET_EXCEEDED"
