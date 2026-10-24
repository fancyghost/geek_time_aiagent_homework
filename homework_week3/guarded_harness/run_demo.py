"""run_demo.py —— 一键跑通四个交付 Demo（离线、确定性、可复现）。

运行方式（在 ``guarded_harness/`` 目录下）::

    python run_demo.py

它不联网、不依赖真实 LLM：用 ``FakeModel`` 按脚本产出工具调用，治理层
（Gate / Sandbox / Completion / Checkpoint / Audit）负责门禁、执行边界、
状态、审计、恢复与最终验收。四个 Demo 分别产出交付所需的记录，并写入
``demo_artifacts/``：

- **Demo 1 完整运行**：代码理解 + 测试修复两条 happy path → ``run_record.json``
  （一份完整运行记录：运行/预算/证据/步骤/审批/审计链）。
- **Demo 2 审计链**：拒绝留痕(A1) + 读放行/写待审批/写批准执行(A2) →
  ``audit_chain.txt``（可读决策链）与 ``audit.jsonl``（append-only 原始日志）。
- **Demo 3 Checkpoint 恢复**：跨实例恢复且预算不清零(C3) + 结果未知禁止自动
  重放(C2) → ``checkpoint_recovery.json``（一份 Checkpoint 恢复记录）。
- **Demo 4 人工审批**：写操作暂停(G3) → 人工批准 → 重放执行 → 任务完成 →
  ``approval_flow.json``。

所有工作区都在系统临时目录中创建并于结束时清理，只有 ``demo_artifacts/``
下的交付记录会保留。**全程不修改 smolagents 源码**（仅通过 ``_bootstrap``
把 ``../smolagents/src`` 只读加入 ``sys.path``）。
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  # 必须在导入 smolagents 之前，确保其源码在 sys.path 上

import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from checkpoint import Checkpoint
from fake_model import FakeModel, ScriptedAction
from harness import GuardedHarness, RunOutcome
from sandbox import make_default_sandbox
from state_store import Budget, ResultStatus, RunState, StepRecord, TaskKind

HERE = Path(__file__).resolve().parent
ARTIFACTS = HERE / "demo_artifacts"

# 命令白名单（与测试保持一致）：仅允许 pytest 相关与 `python -c`。
# `python -B -m pytest` 关闭字节码写入，规避“同秒覆写源码后子进程加载陈旧 .pyc”的竞态。
ALLOWED_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("python", "-c"),
    ("python", "-B", "-m", "pytest"),
    ("pytest",),
    ("python", "-m", "pytest"),
)
DEFAULT_TIMEOUT = 30.0

BUGGY_APP = "def add(a, b):\n    return a - b\n"
FIXED_APP = "def add(a, b):\n    return a + b\n"
TEST_APP = "from app import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
RUN_TEST_CMD = "python -B -m pytest -q test_app.py"


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {title}")
    print("=" * 78)


def _sub(title: str) -> None:
    print(f"\n-- {title} " + "-" * max(0, 60 - len(title)))


def _make_workspace(app: str = BUGGY_APP, test: str | None = TEST_APP) -> Path:
    """在系统临时目录创建一个工作区（含 app.py / test_app.py）。"""

    ws = Path(tempfile.mkdtemp(prefix="gh_demo_"))
    (ws / "app.py").write_text(app, encoding="utf-8")
    if test is not None:
        (ws / "test_app.py").write_text(test, encoding="utf-8")
    return ws


def _make_sandbox(ws: Path):
    return make_default_sandbox(
        ws, allowed_command_prefixes=ALLOWED_PREFIXES, default_timeout_seconds=DEFAULT_TIMEOUT
    )


def _make_harness(
    ws: Path,
    *,
    model: FakeModel,
    task_kind: TaskKind,
    run_id: str,
    checkpoint_dir: Path | None = None,
    audit_path: Path | None = None,
    budget: Budget | None = None,
) -> GuardedHarness:
    return GuardedHarness(
        workspace_root=ws,
        model=model,
        task_kind=task_kind,
        run_id=run_id,
        sandbox=_make_sandbox(ws),
        checkpoint_dir=checkpoint_dir if checkpoint_dir is not None else (ws / ".harness" / "checkpoints"),
        audit_path=audit_path,
        budget=budget,
    )


def _print_outcome(label: str, out: RunOutcome) -> None:
    print(
        f"[{label}] accepted={out.accepted} status={out.status} stop_reason={out.stop_reason} "
        f"steps_used={out.steps_used} writes_used={out.writes_used} resumed={out.resumed}"
    )
    if out.final_answer:
        print(f"          final_answer={out.final_answer!r}")
    if out.reason:
        print(f"          reason={out.reason}")


def _write_json(name: str, payload: Any) -> Path:
    path = ARTIFACTS / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"          → 已写入交付记录：{path.relative_to(HERE)}")
    return path


# --------------------------------------------------------------------------- #
# Demo 1：完整运行（代码理解 + 测试修复）→ 一份完整运行记录
# --------------------------------------------------------------------------- #
def demo_1_full_run() -> dict[str, Any]:
    _hr("Demo 1 · 完整运行（H1/H2 最终验收 + T1 不改 smolagents 接入）")
    summary: dict[str, Any] = {}

    # --- Part A：代码理解（read → final）---
    _sub("Part A · 代码理解：read_file(app.py) → final_answer")
    ws_a = _make_workspace()
    try:
        h_a = _make_harness(
            ws_a,
            model=FakeModel([
                ScriptedAction.call("read_file", tool_call_id="a-read", path="app.py"),
                ScriptedAction.final("app.py 中的 add(a, b) 实际返回 a - b（疑似缺陷）。", tool_call_id="a-final"),
            ]),
            task_kind=TaskKind.CODE_UNDERSTANDING,
            run_id="demo1-understand",
        )
        out_a = h_a.run("请阅读并解释 app.py 中的 add 函数")
        _print_outcome("理解", out_a)
        summary["code_understanding"] = out_a.to_dict()
        assert out_a.accepted and out_a.steps_used == 2, "代码理解 happy path 应被接受且用 2 步"
    finally:
        shutil.rmtree(ws_a, ignore_errors=True)

    # --- Part B：测试修复（预授权写 → read → write → run_test → final）---
    _sub("Part B · 测试修复：预授权 write → run_test 通过 → final（导出完整运行记录）")
    ws_b = _make_workspace()
    try:
        h_b = _make_harness(
            ws_b,
            model=FakeModel([
                ScriptedAction.call("read_file", tool_call_id="b-read", path="test_app.py"),
                ScriptedAction.call("write_file", tool_call_id="b-write", path="app.py", content=FIXED_APP),
                ScriptedAction.call("run_test", tool_call_id="b-test", command=RUN_TEST_CMD),
                ScriptedAction.final("已将 add 修复为 a + b，test_app.py 通过。", tool_call_id="b-final"),
            ]),
            task_kind=TaskKind.TEST_REPAIR,
            run_id="demo1-repair",
        )
        h_b.preapprove("write_file", {"path": "app.py", "content": FIXED_APP}, approval_id="ap-demo1")
        out_b = h_b.run("修复 app.py，使 test_app.py 通过")
        _print_outcome("修复", out_b)
        print(f"          app.py 修复后内容={ (ws_b / 'app.py').read_text(encoding='utf-8')!r}")
        assert out_b.accepted and out_b.steps_used == 4 and out_b.writes_used == 1
        assert (ws_b / "app.py").read_text(encoding="utf-8") == FIXED_APP

        record = h_b.export_run_record()
        _write_json("run_record.json", record)
        print(
            f"          记录概览：steps={len(record['steps'])} evidence={len(record['evidence'])} "
            f"approvals={len(record['approvals'])} audit={len(record['audit_chain'])}"
        )
        summary["test_repair"] = out_b.to_dict()
        summary["run_record_keys"] = sorted(record.keys())
    finally:
        shutil.rmtree(ws_b, ignore_errors=True)

    print("\n[Demo 1] ✅ 两条 happy path 均被 CompletionChecker 接受。")
    return summary


# --------------------------------------------------------------------------- #
# Demo 2：审计链（A1 拒绝留痕 + A2 完整决策链）
# --------------------------------------------------------------------------- #
def demo_2_audit_chain() -> dict[str, Any]:
    _hr("Demo 2 · 审计链（A1 被拒动作留痕 + A2 完整决策链）")
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    audit_path = ARTIFACTS / "audit.jsonl"
    audit_path.unlink(missing_ok=True)  # 每次运行前清空，保证 append-only 日志是本次的完整记录

    ws = _make_workspace()
    try:
        h = _make_harness(
            ws,
            model=FakeModel([
                # 1) 缺 content 的写 → G1 参数校验拒绝（A1：必须留痕）
                ScriptedAction.call("write_file", tool_call_id="tc-bad", path="x.py"),
                # 2) 读放行（A2：GATE/ALLOW → RESULT/EXECUTED）
                ScriptedAction.call("read_file", tool_call_id="tc-read", path="app.py"),
                # 3) 合法但未预授权的写 → 暂停等待审批（A2：APPROVAL/CONFIRM）
                ScriptedAction.call("write_file", tool_call_id="tc-write", path="app.py", content=FIXED_APP),
            ]),
            task_kind=TaskKind.TEST_REPAIR,
            run_id="demo2-audit",
            audit_path=audit_path,
        )
        first = h.run("触发拒绝、读放行、写待审批三种审计路径")
        _print_outcome("首段", first)
        assert first.stop_reason == "waiting_approval"

        # 人工批准待审批写 → 重放执行（A2：GATE/ALLOW → APPROVAL/APPROVED → RESULT/EXECUTED）
        second = h.approve_pending(approval_id="ap-demo2")
        _print_outcome("批准后", second)

        _sub("渲染决策链（按 tool_call_id）")
        blocks: list[str] = []
        for label, cid in (
            ("A1 · 缺参数写被 G1 拒绝", "tc-bad"),
            ("A2 · 读放行", "tc-read"),
            ("A2 · 写：待审批 → 批准 → 执行", "tc-write"),
        ):
            chain = h.audit.chain(cid)
            rendered = h.audit.render_chain(chain)
            print(f"\n【{label}】tool_call_id={cid}（{len(chain)} 条）")
            print(rendered)
            blocks.append(f"### {label} (tool_call_id={cid})\n{rendered}\n")

        (ARTIFACTS / "audit_chain.txt").write_text("\n".join(blocks), encoding="utf-8")
        print(f"\n          → 已写入交付记录：{(ARTIFACTS / 'audit_chain.txt').relative_to(HERE)}")
        print(f"          → 已写入原始 append-only 日志：{audit_path.relative_to(HERE)}")

        # A1 断言：被拒动作确有留痕，首条即 GATE/DENY/INVALID_ARGUMENT。
        bad_chain = h.audit.chain("tc-bad")
        assert bad_chain and bad_chain[0].rule == "INVALID_ARGUMENT" and bad_chain[0].decision == "deny"
        summary = {
            "denied_tool_call": "tc-bad",
            "denied_first_rule": bad_chain[0].rule,
            "read_chain_len": len(h.audit.chain("tc-read")),
            "write_chain_len": len(h.audit.chain("tc-write")),
            "audit_jsonl_lines": len(audit_path.read_text(encoding="utf-8").strip().splitlines()),
        }
    finally:
        shutil.rmtree(ws, ignore_errors=True)

    print("\n[Demo 2] ✅ 拒绝、放行、审批、执行全部留痕，决策链可按 tool_call_id 还原。")
    return summary


# --------------------------------------------------------------------------- #
# Demo 3：Checkpoint 恢复（C3 预算不清零 + C2 结果未知禁止重放）
# --------------------------------------------------------------------------- #
def demo_3_checkpoint_recovery() -> dict[str, Any]:
    _hr("Demo 3 · Checkpoint 恢复（C3 跨实例恢复预算不清零 + C2 结果未知禁止重放）")
    summary: dict[str, Any] = {}

    # --- C3：同一 run_id / checkpoint_dir，两个实例（等价于两个进程）---
    _sub("C3 · 实例1 跑满会话步数上限 → 实例2 从 Checkpoint 恢复并收尾")
    ws = _make_workspace(app="a = 1\n", test=None)
    (ws / "b.py").write_text("b = 2\n", encoding="utf-8")
    ck = ws / ".harness" / "checkpoints"
    try:
        h1 = _make_harness(
            ws,
            model=FakeModel([
                ScriptedAction.call("read_file", tool_call_id="c3-r1", path="a.py"),
                ScriptedAction.call("read_file", tool_call_id="c3-r2", path="b.py"),
                ScriptedAction.final("不该到这里"),
            ]),
            task_kind=TaskKind.CODE_UNDERSTANDING,
            run_id="demo3-resume",
            checkpoint_dir=ck,
        )
        partial = h1.run("理解 a.py 与 b.py", session_step_limit=2)  # 只允许 2 步
        _print_outcome("实例1", partial)
        assert partial.stop_reason == "max_steps" and partial.steps_used == 2

        # 新实例（新进程）从同一 Checkpoint 恢复，仅带“收尾”动作。
        h2 = _make_harness(
            ws,
            model=FakeModel([ScriptedAction.final("a=1，b=2。", tool_call_id="c3-final")]),
            task_kind=TaskKind.CODE_UNDERSTANDING,
            run_id="demo3-resume",
            checkpoint_dir=ck,
        )
        resumed = h2.resume()
        _print_outcome("实例2", resumed)
        assert resumed.resumed and resumed.accepted and resumed.stop_reason == "completed"
        assert resumed.steps_used == 3, "预算计数必须跨实例累加（2 + 1），不得清零"

        record = h2.export_run_record()
        _write_json("checkpoint_recovery.json", {
            "c3_cross_instance_resume": {
                "instance_1_partial": partial.to_dict(),
                "instance_2_resumed": resumed.to_dict(),
                "recovery_report": record["recovery"],
                "budget_after_resume": record["budget"],
            },
        })
        summary["c3"] = {"instance_1": partial.to_dict(), "instance_2": resumed.to_dict()}
    finally:
        shutil.rmtree(ws, ignore_errors=True)

    # --- C2：写意图已登记但结果未知（PENDING）→ 恢复时禁止自动重放 ---
    _sub("C2 · 构造“写结果未知”的 Checkpoint → resume 判为 unknown_result 且不重放")
    ws2 = _make_workspace()
    try:
        ck2 = ws2 / ".harness" / "checkpoints"
        st = RunState(
            run_id="demo3-c2", task="修复（模拟崩溃）", task_kind=TaskKind.TEST_REPAIR,
            workspace_root=str(ws2), budget=Budget(steps_used=1),
        )
        st.steps.append(StepRecord(
            step_number=1, tool_call_id="c2-write", tool_name="write_file",
            result_status=ResultStatus.PENDING, is_write=True, idempotency_key="k",
        ))
        Checkpoint(ck2, fingerprint_fn=_make_sandbox(ws2).workspace_fingerprint).save(st)

        h = _make_harness(
            ws2, model=FakeModel([]), task_kind=TaskKind.TEST_REPAIR,
            run_id="demo3-c2", checkpoint_dir=ck2,
        )
        out = h.resume()
        _print_outcome("C2 恢复", out)
        assert out.stop_reason == "unknown_result" and out.recovery is not None
        assert "c2-write" in out.recovery["unknown_result_tool_calls"]
        assert h.state.budget.writes_used == 0, "结果未知的写禁止自动重放"

        # 追加写入交付记录（与 C3 合并到同一文件）。
        path = ARTIFACTS / "checkpoint_recovery.json"
        merged = json.loads(path.read_text(encoding="utf-8"))
        merged["c2_unknown_result_no_replay"] = {
            "outcome": out.to_dict(),
            "recovery_report": out.recovery,
        }
        path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        summary["c2"] = out.to_dict()
    finally:
        shutil.rmtree(ws2, ignore_errors=True)

    print("\n[Demo 3] ✅ 跨实例恢复预算不清零；结果未知的写禁止自动重放。")
    return summary


# --------------------------------------------------------------------------- #
# Demo 4：人工审批（G3 暂停 → 批准 → 重放执行 → 完成）
# --------------------------------------------------------------------------- #
def demo_4_approval() -> dict[str, Any]:
    _hr("Demo 4 · 人工审批（G3：写操作暂停 → 人工批准 → 重放执行 → 完成）")
    ws = _make_workspace()
    try:
        h = _make_harness(
            ws,
            model=FakeModel([
                ScriptedAction.call("write_file", tool_call_id="d-write", path="app.py", content=FIXED_APP),
                ScriptedAction.call("run_test", tool_call_id="d-test", command=RUN_TEST_CMD),
                ScriptedAction.final("已修复并通过测试。", tool_call_id="d-final"),
            ]),
            task_kind=TaskKind.TEST_REPAIR,
            run_id="demo4-approval",
        )
        _sub("阶段 1 · 未授权写入 → 暂停等待审批")
        print(f"          写入前 app.py={ (ws / 'app.py').read_text(encoding='utf-8')!r}")
        first = h.run("修复 app.py（写入需人工审批）")
        _print_outcome("暂停", first)
        assert first.stop_reason == "waiting_approval" and h.state.pending_action is not None
        assert (ws / "app.py").read_text(encoding="utf-8") == BUGGY_APP, "暂停期间不得写入"
        print(f"          暂停时 app.py 仍未改动={ (ws / 'app.py').read_text(encoding='utf-8')!r}")

        _sub("阶段 2 · 人工批准 → 经 Gate 重放写入 → 跑测试 → 完成")
        second = h.approve_pending(approval_id="ap-demo4")
        _print_outcome("完成", second)
        assert second.accepted and second.stop_reason == "completed" and second.steps_used == 4
        assert (ws / "app.py").read_text(encoding="utf-8") == FIXED_APP
        print(f"          批准后 app.py={ (ws / 'app.py').read_text(encoding='utf-8')!r}")

        _write_json("approval_flow.json", {
            "pending_before_approval": h.state.pending_action,
            "phase_1_paused": first.to_dict(),
            "phase_2_after_approval": second.to_dict(),
            "write_chain": [r.to_dict() for r in h.audit.chain("d-write")],
        })
        summary = {"phase_1": first.to_dict(), "phase_2": second.to_dict()}
    finally:
        shutil.rmtree(ws, ignore_errors=True)

    print("\n[Demo 4] ✅ 未授权写入被安全暂停；人工批准后经 Gate 重放执行并完成任务。")
    return summary


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main() -> int:
    try:  # 保证中文在 Windows 控制台（cp936/cp1252）下也能正确输出。
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

    # 清空并重建交付目录，保证每次运行的记录都是完整、可复现的。
    shutil.rmtree(ARTIFACTS, ignore_errors=True)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)

    print("Guarded Coding Harness · 交付 Demo（离线 / 确定性 / 不修改 smolagents 源码）")
    print(f"作业代码目录：{HERE}")
    print(f"交付记录目录：{ARTIFACTS}")

    results: dict[str, Any] = {}
    demos = (
        ("demo_1_full_run", demo_1_full_run),
        ("demo_2_audit_chain", demo_2_audit_chain),
        ("demo_3_checkpoint_recovery", demo_3_checkpoint_recovery),
        ("demo_4_approval", demo_4_approval),
    )
    for name, fn in demos:
        results[name] = fn()

    _write_json("summary.json", results)

    _hr("全部 Demo 通过 · 交付清单")
    print("  源码与测试         : guarded_harness/*.py + guarded_harness/tests/*.py")
    print("  pytest 结果        : 见 README（101 passed）")
    print("  完整运行记录       : demo_artifacts/run_record.json")
    print("  审计记录           : demo_artifacts/audit_chain.txt + demo_artifacts/audit.jsonl")
    print("  Checkpoint 恢复记录: demo_artifacts/checkpoint_recovery.json")
    print("  人工审批记录       : demo_artifacts/approval_flow.json")
    print("\nALL_DEMOS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
