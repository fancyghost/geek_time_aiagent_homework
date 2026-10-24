"""tests/test_audit.py —— append-only 审计（A1 拒绝留痕 / A2 完整决策链）与日志语义。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from audit import AuditActor, AuditDecision, AuditLog, AuditPhase, AuditRecord
from gate import GateRequest
from state_store import args_digest


# --------------------------------------------------------------------------- #
# 日志基本语义
# --------------------------------------------------------------------------- #
def test_seq_monotonic_and_len(audit):
    r1 = audit.append(run_id="r", phase=AuditPhase.GATE, decision=AuditDecision.ALLOW, rule="A")
    r2 = audit.append(run_id="r", phase=AuditPhase.RESULT, decision=AuditDecision.EXECUTED, rule="B")
    assert r1.seq == 1 and r2.seq == 2
    assert len(audit) == 2


def test_record_is_immutable(audit):
    rec = audit.append(run_id="r", phase=AuditPhase.GATE, decision=AuditDecision.ALLOW, rule="A")
    assert isinstance(rec, AuditRecord)
    with pytest.raises(FrozenInstanceError):
        rec.rule = "hacked"  # type: ignore[misc]


def test_by_run_and_by_tool_call(audit):
    audit.append(run_id="r1", phase=AuditPhase.GATE, decision=AuditDecision.ALLOW, rule="A", tool_call_id="t1")
    audit.append(run_id="r2", phase=AuditPhase.GATE, decision=AuditDecision.ALLOW, rule="A", tool_call_id="t2")
    assert len(audit.by_run("r1")) == 1
    assert len(audit.by_tool_call("t2")) == 1


def test_chain_sorted_by_seq(audit):
    audit.append(run_id="r", phase=AuditPhase.RESULT, decision=AuditDecision.EXECUTED, rule="C", tool_call_id="t")
    audit.append(run_id="r", phase=AuditPhase.GATE, decision=AuditDecision.ALLOW, rule="A", tool_call_id="t")
    chain = audit.chain("t")
    assert [r.seq for r in chain] == sorted(r.seq for r in chain)


def test_find_filters(audit):
    audit.append(run_id="r", phase=AuditPhase.GATE, decision=AuditDecision.DENY, rule="INVALID_ARGUMENT")
    audit.append(run_id="r", phase=AuditPhase.GATE, decision=AuditDecision.ALLOW, rule="ALLOWED")
    denies = audit.find(decision=AuditDecision.DENY)
    assert len(denies) == 1 and denies[0].rule == "INVALID_ARGUMENT"
    assert len(audit.find(phase=AuditPhase.GATE)) == 2


def test_render_chain_text(audit):
    audit.append(run_id="r", phase=AuditPhase.GATE, decision=AuditDecision.ALLOW, rule="ALLOWED", tool_name="read_file")
    text = audit.render_chain(audit.records)
    assert "gate/allow" in text and "read_file" in text


def test_jsonl_persist_and_reload_continues_seq(tmp_path):
    path = tmp_path / "audit.jsonl"
    log1 = AuditLog(path=path)
    log1.append(run_id="r", phase=AuditPhase.GATE, decision=AuditDecision.ALLOW, rule="A")
    log1.append(run_id="r", phase=AuditPhase.GATE, decision=AuditDecision.ALLOW, rule="B")
    # 新进程重新打开：续接最大 seq，且以追加方式写入（不覆盖既有行）。
    log2 = AuditLog(path=path)
    rec = log2.append(run_id="r", phase=AuditPhase.RESULT, decision=AuditDecision.EXECUTED, rule="C")
    assert rec.seq == 3
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    reloaded = AuditLog.load_jsonl(path)
    assert [r["seq"] for r in reloaded] == [1, 2, 3]


def test_in_memory_log_has_no_file():
    log = AuditLog()  # 无持久化路径
    log.append(run_id="r", phase=AuditPhase.STATE, decision=AuditDecision.RECORDED, rule="X")
    assert len(log) == 1


# --------------------------------------------------------------------------- #
# A1：被拒动作必须留痕（含发起者、阶段、规则）
# --------------------------------------------------------------------------- #
def test_a1_denied_action_leaves_trail(gate, audit):
    # write_file 缺 content → G1 拒绝；决策链首条即 GATE/INVALID_ARGUMENT/DENY，责任方为模型。
    res = gate.execute(GateRequest(tool_call_id="tc-a1", tool_name="write_file", arguments={"path": "x.py"}))
    assert res.ok is False
    chain = audit.chain("tc-a1")
    assert chain, "被拒动作必须留下审计"
    first = chain[0]
    assert first.phase == str(AuditPhase.GATE)
    assert first.rule == "INVALID_ARGUMENT"
    assert first.decision == str(AuditDecision.DENY)
    assert first.actor == str(AuditActor.MODEL)
    assert first.tool_call_id == "tc-a1"


# --------------------------------------------------------------------------- #
# A2：完整决策链（读放行 / 写待审批 / 写批准后执行）
# --------------------------------------------------------------------------- #
def test_a2_read_allowed_chain(gate, audit, workspace):
    (workspace / "n.txt").write_text("hi", encoding="utf-8")
    gate.execute(GateRequest(tool_call_id="tc-read", tool_name="read_file", arguments={"path": "n.txt"}))
    rules = [(r.phase, r.decision, r.rule) for r in audit.chain("tc-read")]
    assert (str(AuditPhase.GATE), str(AuditDecision.ALLOW), "ALLOWED") in rules
    assert (str(AuditPhase.RESULT), str(AuditDecision.EXECUTED), "OK") in rules


def test_a2_unapproved_write_waits_chain(gate, audit):
    gate.execute(GateRequest(tool_call_id="tc-wait", tool_name="write_file",
                             arguments={"path": "o.py", "content": "1"}))
    phases = [(r.phase, r.decision) for r in audit.chain("tc-wait")]
    assert (str(AuditPhase.APPROVAL), str(AuditDecision.CONFIRM)) in phases


def test_a2_approved_write_full_chain(gate, store, audit):
    args = {"path": "ok.py", "content": "z"}
    digest = args_digest("write_file", args)
    store.grant_approval("ap-a2", "write_file", digest, actor="user")
    gate.execute(GateRequest(tool_call_id="tc-ok", tool_name="write_file", arguments=args))
    seq = [(r.phase, r.decision) for r in audit.chain("tc-ok")]
    assert (str(AuditPhase.GATE), str(AuditDecision.ALLOW)) in seq
    assert (str(AuditPhase.APPROVAL), str(AuditDecision.APPROVED)) in seq
    assert (str(AuditPhase.RESULT), str(AuditDecision.EXECUTED)) in seq
    # 审批记录的责任方是授权者（user）。
    approved = [r for r in audit.chain("tc-ok") if r.decision == str(AuditDecision.APPROVED)]
    assert approved and approved[0].actor == str(AuditActor.USER)
