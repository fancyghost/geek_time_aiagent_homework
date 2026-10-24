"""tests/test_harness_loop.py —— 端到端主循环（T1 不改 smolagents 接入 / T2 tool_call_id 对齐 + 恢复/审批/幂等/预算集成）。"""

from __future__ import annotations

from conftest import BUGGY_APP, FIXED_APP, TEST_APP

from checkpoint import Checkpoint
from fake_model import FakeModel, ScriptedAction
from state_store import Budget, ResultStatus, RunState, StepRecord, TaskKind


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_happy_path_code_understanding(make_harness, workspace):
    (workspace / "app.py").write_text(BUGGY_APP, encoding="utf-8")
    h = make_harness(
        [
            ScriptedAction.call("read_file", path="app.py"),
            ScriptedAction.final("app.py 里的 add 返回 a - b。"),
        ],
        task_kind=TaskKind.CODE_UNDERSTANDING,
        run_id="hp-understand",
    )
    out = h.run("解释 add 函数")
    assert out.accepted is True
    assert out.stop_reason == "completed"
    assert out.status == "completed"
    assert out.steps_used == 2
    assert out.final_answer.startswith("app.py")


def test_happy_path_test_repair_preapproved(make_harness, app_workspace):
    h = make_harness(
        [
            ScriptedAction.call("read_file", path="test_app.py"),
            ScriptedAction.call("write_file", path="app.py", content=FIXED_APP),
            ScriptedAction.call("run_test", command="python -B -m pytest -q test_app.py"),
            ScriptedAction.final("已修复 add 为 a + b，测试通过。"),
        ],
        task_kind=TaskKind.TEST_REPAIR,
        run_id="hp-repair",
    )
    h.preapprove("write_file", {"path": "app.py", "content": FIXED_APP})
    out = h.run("修复 app.py 使测试通过")
    assert out.accepted is True and out.stop_reason == "completed"
    assert out.steps_used == 4
    assert out.writes_used == 1
    assert (app_workspace / "app.py").read_text(encoding="utf-8") == FIXED_APP


# --------------------------------------------------------------------------- #
# G3：审批暂停 → 人工批准 → 继续
# --------------------------------------------------------------------------- #
def test_approval_pause_then_approve_pending(make_harness, app_workspace):
    h = make_harness(
        [
            ScriptedAction.call("write_file", path="app.py", content=FIXED_APP),
            ScriptedAction.call("run_test", command="python -B -m pytest -q test_app.py"),
            ScriptedAction.final("修复完成。"),
        ],
        task_kind=TaskKind.TEST_REPAIR,
        run_id="approve-run",
    )
    first = h.run("修复（写入需审批）")
    assert first.accepted is False
    assert first.stop_reason == "waiting_approval"
    assert first.status == "waiting_approval"
    assert h.state.pending_action is not None
    # 尚未写入。
    assert (app_workspace / "app.py").read_text(encoding="utf-8") == BUGGY_APP

    second = h.approve_pending()
    assert second.accepted is True and second.stop_reason == "completed"
    assert second.steps_used == 4  # 写(尝试) + 写(重放) + 跑测试 + 收尾
    assert (app_workspace / "app.py").read_text(encoding="utf-8") == FIXED_APP


def test_approve_pending_without_pending_is_safe(make_harness, workspace):
    (workspace / "a.py").write_text(BUGGY_APP, encoding="utf-8")
    h = make_harness([ScriptedAction.call("read_file", path="a.py"),
                      ScriptedAction.final("ok")], run_id="no-pending")
    h.run("读一下")
    out = h.approve_pending()  # 没有待审批动作
    assert out.accepted is False


# --------------------------------------------------------------------------- #
# G4：幂等——重复写被拒但任务仍可完成
# --------------------------------------------------------------------------- #
def test_idempotent_second_write_denied(make_harness, app_workspace):
    h = make_harness(
        [
            ScriptedAction.call("write_file", path="app.py", content=FIXED_APP),
            ScriptedAction.call("write_file", path="app.py", content=FIXED_APP),  # 重复写 → G4
            ScriptedAction.call("run_test", command="python -B -m pytest -q test_app.py"),
            ScriptedAction.final("修复完成。"),
        ],
        task_kind=TaskKind.TEST_REPAIR,
        run_id="idem-run",
    )
    h.preapprove("write_file", {"path": "app.py", "content": FIXED_APP})
    out = h.run("修复")
    assert out.accepted is True
    assert out.writes_used == 1  # 只真正写了一次
    assert h.audit.find(rule="ALREADY_EXECUTED")  # 第二次被幂等拒绝并留痕


# --------------------------------------------------------------------------- #
# H1/H2：过早 final 被拒 → 反馈 → 补齐后接受
# --------------------------------------------------------------------------- #
def test_premature_final_rejected_then_recovered(make_harness, app_workspace):
    h = make_harness(
        [
            ScriptedAction.final("我修好了"),  # 过早：尚无交付物/测试
            ScriptedAction.call("read_file", path="test_app.py"),
            ScriptedAction.call("write_file", path="app.py", content=FIXED_APP),
            ScriptedAction.call("run_test", command="python -B -m pytest -q test_app.py"),
            ScriptedAction.final("真的修好了"),
        ],
        task_kind=TaskKind.TEST_REPAIR,
        run_id="reject-run",
    )
    h.preapprove("write_file", {"path": "app.py", "content": FIXED_APP})
    out = h.run("修复")
    assert out.accepted is True and out.stop_reason == "completed"
    assert out.steps_used == 5
    assert out.final_answer == "真的修好了"
    assert h.audit.find(rule="COMPLETION_REJECTED")  # 过早的 final 被拒并留痕


# --------------------------------------------------------------------------- #
# C3：跨实例恢复，预算计数不清零
# --------------------------------------------------------------------------- #
def test_checkpoint_resume_preserves_budget(make_harness, workspace, tmp_path):
    (workspace / "a.py").write_text("a = 1\n", encoding="utf-8")
    (workspace / "b.py").write_text("b = 2\n", encoding="utf-8")
    ck = tmp_path / "shared_ck"
    ap = tmp_path / "shared_audit.jsonl"

    h1 = make_harness(
        [
            ScriptedAction.call("read_file", path="a.py"),
            ScriptedAction.call("read_file", path="b.py"),
            ScriptedAction.final("不该到这"),
        ],
        task_kind=TaskKind.CODE_UNDERSTANDING,
        run_id="resume-run",
        checkpoint_dir=ck,
        audit_path=ap,
    )
    partial = h1.run("理解 a 和 b", session_step_limit=2)  # 只允许 2 步 → MAX_STEPS
    assert partial.accepted is False
    assert partial.stop_reason == "max_steps"
    assert partial.steps_used == 2

    # 新实例（等价于新进程）从同一 Checkpoint 恢复，仅带“收尾”动作。
    h2 = make_harness(
        model=FakeModel([ScriptedAction.final("a=1, b=2")]),
        task_kind=TaskKind.CODE_UNDERSTANDING,
        run_id="resume-run",
        checkpoint_dir=ck,
        audit_path=ap,
    )
    resumed = h2.resume()
    assert resumed.resumed is True
    assert resumed.accepted is True and resumed.stop_reason == "completed"
    assert resumed.steps_used == 3  # 2（前次）+ 1（本次），未清零
    assert resumed.final_answer == "a=1, b=2"


def test_resume_without_checkpoint_is_safe(make_harness, tmp_path):
    h = make_harness(model=FakeModel([]), run_id="ghost", checkpoint_dir=tmp_path / "empty_ck")
    out = h.resume()
    assert out.accepted is False
    assert out.stop_reason == "interrupted"


# --------------------------------------------------------------------------- #
# C2：结果未知的写入 → 恢复时禁止自动重放
# --------------------------------------------------------------------------- #
def test_unknown_result_write_blocks_auto_replay(make_harness, workspace, tmp_path):
    ck = tmp_path / "c2_ck"
    # 直接构造一个“写意图已登记但结果未知（PENDING）”的 Checkpoint，模拟执行到一半崩溃。
    st = RunState(run_id="c2-run", task="修复", task_kind=TaskKind.TEST_REPAIR,
                  workspace_root=str(workspace), budget=Budget(steps_used=1))
    st.steps.append(StepRecord(step_number=1, tool_call_id="tc-write", tool_name="write_file",
                               result_status=ResultStatus.PENDING, is_write=True, idempotency_key="k"))
    Checkpoint(ck).save(st)

    h = make_harness(model=FakeModel([]), run_id="c2-run", checkpoint_dir=ck)
    out = h.resume()
    assert out.accepted is False
    assert out.stop_reason == "unknown_result"
    assert out.recovery is not None
    assert "tc-write" in out.recovery["unknown_result_tool_calls"]
    # 未自动重放：没有新的成功写入步骤。
    assert h.state.budget.writes_used == 0


# --------------------------------------------------------------------------- #
# H3：预算 / 会话步数上限 / 模型耗尽
# --------------------------------------------------------------------------- #
def test_budget_exhausted_stops(make_harness, workspace):
    (workspace / "a.py").write_text("a = 1\n", encoding="utf-8")
    h = make_harness(
        [ScriptedAction.call("read_file", path="a.py"), ScriptedAction.final("x")],
        run_id="budget-run",
        budget=Budget(max_steps=1),
    )
    out = h.run("读")
    assert out.accepted is False
    assert out.stop_reason == "budget_exhausted"


def test_session_step_limit_stops(make_harness, workspace):
    for name in ("f1.py", "f2.py", "f3.py"):
        (workspace / name).write_text("x = 1\n", encoding="utf-8")
    h = make_harness(
        [
            ScriptedAction.call("read_file", path="f1.py"),
            ScriptedAction.call("read_file", path="f2.py"),
            ScriptedAction.call("read_file", path="f3.py"),
            ScriptedAction.final("done"),
        ],
        run_id="limit-run",
        budget=Budget(max_steps=10),
    )
    out = h.run("读多个文件", session_step_limit=2)
    assert out.accepted is False
    assert out.stop_reason == "max_steps"
    assert out.steps_used == 2


def test_model_exhausted_stops(make_harness, workspace):
    (workspace / "a.py").write_text("a = 1\n", encoding="utf-8")
    h = make_harness([ScriptedAction.call("read_file", path="a.py")], run_id="exhaust-run")
    # 脚本只有一个读动作，没有 final：读完后模型耗尽 → MODEL_ERROR。
    out = h.run("读")
    assert out.accepted is False
    assert out.stop_reason == "model_error"


# --------------------------------------------------------------------------- #
# T2：tool_call_id 贯穿审计链；导出完整运行记录
# --------------------------------------------------------------------------- #
def test_tool_call_id_aligns_audit_chain(make_harness, workspace):
    (workspace / "a.py").write_text("a = 1\n", encoding="utf-8")
    h = make_harness(
        [
            ScriptedAction.call("read_file", tool_call_id="cid-read", path="a.py"),
            ScriptedAction.final("ok", tool_call_id="cid-final"),
        ],
        run_id="cid-run",
    )
    h.run("读")
    chain = h.audit.chain("cid-read")
    assert chain and all(r.tool_call_id == "cid-read" for r in chain)
    assert h.audit.chain("cid-final")


def test_export_run_record_shape(make_harness, workspace):
    (workspace / "a.py").write_text("a = 1\n", encoding="utf-8")
    h = make_harness(
        [ScriptedAction.call("read_file", path="a.py"), ScriptedAction.final("done")],
        run_id="export-run",
    )
    h.run("读")
    rec = h.export_run_record()
    assert rec["run"]["run_id"] == "export-run"
    assert rec["run"]["status"] == "completed"
    assert rec["budget"]["steps_used"] == 2
    assert isinstance(rec["evidence"], list) and rec["evidence"]
    assert isinstance(rec["steps"], list) and rec["steps"]
    assert isinstance(rec["audit_chain"], list) and rec["audit_chain"]
    assert rec["recovery"] is None
