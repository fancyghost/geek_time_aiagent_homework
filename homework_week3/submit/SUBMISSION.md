# 最终提交（Final Submission）· Guarded Coding Harness

本目录 `homework_week3/submit/` 是 **自包含的最终提交包**：源码、测试、`pytest -q` 实际结果、
完整运行记录、审计记录、Checkpoint 恢复记录齐备，可独立复现。

> **前置约束（务必阅读）**：本包 **不含也不修改** smolagents 源码，仅通过 `_bootstrap.py` 以
> `sys.path` **只读引用** 上游源码。因此复现时 `submit/` 必须与 `smolagents/` **同为
> `homework_week3/` 的子目录**（即保持当前相对位置 `../smolagents/src`）。

---

## 一、交付清单对照（题目要求 → 本包文件）

| # | 题目要求 | 交付文件 | 说明 |
|:-:|----------|----------|------|
| 1 | **源码与测试** | `*.py`（12 个源文件）+ `tests/`（7 个文件） | 见 §二 目录结构 |
| 2 | **pytest -q 实际结果** | [`pytest_result.txt`](pytest_result.txt) | `101 passed`，见 §三 |
| 3 | **至少一条完整运行记录** | [`demo_artifacts/run_record.json`](demo_artifacts/run_record.json) | 测试修复完整运行，见 §四 |
| 4 | **一份审计记录** | [`demo_artifacts/audit_chain.txt`](demo_artifacts/audit_chain.txt) + [`demo_artifacts/audit.jsonl`](demo_artifacts/audit.jsonl) | 可读决策链 + append-only 原始日志，见 §五 |
| 5 | **一份 Checkpoint 恢复记录** | [`demo_artifacts/checkpoint_recovery.json`](demo_artifacts/checkpoint_recovery.json) | C3 跨实例恢复 + C2 禁止重放，见 §六 |

> 附加佐证：`demo_artifacts/approval_flow.json`（G3 人工审批流水）、`demo_artifacts/summary.json`（四个 Demo 汇总）。

---

## 二、目录结构

```
submit/
├── SUBMISSION.md          # 本文件：提交说明与交付清单
├── README.md              # 设计文档（架构 / 规则 / 验收对照）
├── pytest.ini             # testpaths=tests；norecursedirs 排除 smolagents
├── pytest_result.txt      # ← 交付项 2：pytest -q 实际结果
│
├── _bootstrap.py          # sys.path 引导（只读引用 ../smolagents/src，不改源码）
├── audit.py               # append-only 审计（A1/A2）
├── state_store.py         # 运行状态 / 预算 / 证据 / 步骤 / 审批 / 幂等键
├── sandbox.py             # 执行边界：路径/软链/敏感目录/命令白名单/超时/环境/输出（S1-S4）
├── catalog.py             # 工具目录：严格参数模型 + 哑 handler + 证据类型
├── gate.py                # 唯一入口门禁（G1-G4）
├── completion.py          # 最终验收判定（H1-H3）
├── checkpoint.py          # 原子保存 / 恢复 / 对账（C1-C4）
├── coding_tools.py        # smolagents 适配层：GuardedTool / GuardedToolCallingAgent（T1/T2）
├── fake_model.py          # 确定性脚本模型（离线可复现）
├── harness.py             # 主循环编排：装配 + 驱动 ReAct + 预算/审批/恢复/验收
├── run_demo.py            # 一键生成 §四~§六 的交付记录
│
├── tests/                 # ← 交付项 1（测试）：101 用例
│   ├── conftest.py
│   ├── test_sandbox.py            # S1-S4 + 输出/环境边界 + 指纹
│   ├── test_state_checkpoint.py   # 状态存储 + C1-C4
│   ├── test_gate.py               # G1-G4
│   ├── test_audit.py              # A1/A2 + append-only 语义
│   ├── test_completion.py         # H1-H3
│   └── test_harness_loop.py       # 端到端 T1/T2 + 集成路径
│
└── demo_artifacts/        # ← 交付项 3/4/5：run_demo.py 产出的记录（UTF-8）
    ├── run_record.json            # 完整运行记录
    ├── audit_chain.txt            # 审计记录（可读决策链）
    ├── audit.jsonl                # 审计记录（append-only 原始日志，11 行）
    ├── checkpoint_recovery.json   # Checkpoint 恢复记录
    ├── approval_flow.json         # 人工审批流水（附加）
    └── summary.json               # 四个 Demo 汇总（附加）
```

---

## 三、交付项 2 · pytest -q 实际结果

在 `submit/` 目录下执行 `python -m pytest -q`，实际输出（完整内容见 [`pytest_result.txt`](pytest_result.txt)）：

```
........................................................................ [ 71%]
.............................                                            [100%]
101 passed in 4.27s
```

**101 个用例全部通过**，覆盖 S1-S4 / C1-C4 / G1-G4 / A1-A2 / H1-H3 / T1-T2 全部验收点。

---

## 四、交付项 3 · 完整运行记录（run_record.json 摘录）

一次「测试修复」任务的完整运行记录（`read_file → write_file → run_test → final_answer`）：

```jsonc
{
  "run": {
    "run_id": "demo1-repair", "task_kind": "test_repair",
    "status": "completed", "stop_reason": "completed",
    "final_answer": "已将 add 修复为 a + b，test_app.py 通过。",
    "workspace_fingerprint": "bdf8e1a4…"
  },
  "budget": { "max_steps": 12, "steps_used": 4, "writes_used": 1, "seconds_used": 0.472 },
  "evidence": [
    { "kind": "file_read",   "ref": "test_app.py", "tool_call_id": "b-read"  },
    { "kind": "deliverable", "ref": "app.py",      "tool_call_id": "b-write" },
    { "kind": "test_result", "ref": "python -B -m pytest -q test_app.py",
      "detail": { "returncode": 0, "passed": true, "stdout_tail": "1 passed in 0.01s" },
      "tool_call_id": "b-test" }        // 与交付物同指纹 → 证明「当前版本通过」(H2)
  ],
  "steps":  [ /* 4 步，各带 tool_call_id / args_digest / decision / result_status */ ],
  "approvals": [ { "approval_id": "ap-demo1", "tool_name": "write_file", "consumed": true } ],
  "audit_chain": [ /* 12 条：APPROVAL_GRANTED, RUN_STARTED, gate/result×4, RUN_FINISHED */ ],
  "recovery": null
}
```

要点：模型的 `final_answer` 只是「声称完成」，最终由 `CompletionChecker` 依据 **交付物 + 当前指纹下通过的测试** 判定 `accepted=true`（H1/H2）。

---

## 五、交付项 4 · 审计记录（audit_chain.txt 全文）

按 `tool_call_id` 还原的完整决策链（原始 append-only 日志见 `audit.jsonl`）：

```
### A1 · 缺参数写被 G1 拒绝 (tool_call_id=tc-bad)
#  2 [gate/deny] rule=INVALID_ARGUMENT actor=model tool_call_id=tc-bad tool=write_file :: 参数校验失败

### A2 · 读放行 (tool_call_id=tc-read)
#  3 [gate/allow] rule=ALLOWED actor=model tool_call_id=tc-read tool=read_file :: 全部前置检查通过
#  4 [result/executed] rule=OK actor=harness tool_call_id=tc-read tool=read_file :: handler executed

### A2 · 写：待审批 → 批准 → 执行 (tool_call_id=tc-write)
#  5 [approval/confirm] rule=WAITING_APPROVAL actor=model tool_call_id=tc-write tool=write_file :: 写操作等待人工审批：app.py
#  8 [gate/allow] rule=ALLOWED actor=user tool_call_id=tc-write tool=write_file :: 全部前置检查通过
#  9 [approval/approved] rule=APPROVED actor=user tool_call_id=tc-write tool=write_file :: 审批 ap-demo2 已消费
# 10 [result/executed] rule=OK actor=harness tool_call_id=tc-write tool=write_file :: handler executed
```

要点：**A1** 被拒动作（缺 `content`）留痕，首条即 `gate/deny · INVALID_ARGUMENT`，责任方 `model`；
**A2** 写操作的完整链路「待审批 → 人工批准（责任方转为 `user`）→ 执行」可逐条还原；记录不可变、`seq` 单调递增。

---

## 六、交付项 5 · Checkpoint 恢复记录（checkpoint_recovery.json 摘录）

```jsonc
{
  "c3_cross_instance_resume": {                 // C3：跨实例恢复，预算计数不清零
    "instance_1_partial": { "stop_reason": "max_steps", "steps_used": 2, "accepted": false },
    "instance_2_resumed": { "stop_reason": "completed", "steps_used": 3, "accepted": true, "resumed": true },
    "recovery_report":    { "resumed_from_step": 2, "workspace_changed": false },
    "budget_after_resume":{ "max_steps": 12, "steps_used": 3 }   // 2（前次）+ 1（本次），未清零
  },
  "c2_unknown_result_no_replay": {              // C2：结果未知的写 → 禁止自动重放
    "outcome":         { "stop_reason": "unknown_result", "writes_used": 0, "accepted": false,
                         "reason": "存在结果未知的写操作（1 个），禁止自动重放" },
    "recovery_report": { "unknown_result_tool_calls": ["c2-write"], "notes": ["unknown_result_writes=1"] }
  }
}
```

要点：实例 1 跑满会话步数上限（`max_steps`，2 步）后停机；**新实例（等价新进程）** 从同一 Checkpoint
`resume`，仅带「收尾」动作即完成，`steps_used=3` 证明 **预算跨实例累加、不清零（C3）**；
另构造「写意图已登记但结果未知（PENDING）」的快照，`resume` 判为 `unknown_result` 且 `writes_used=0`，
证明 **结果未知的写禁止自动重放（C2）**。

---

## 七、如何复现

在 `homework_week3/submit/` 目录下执行（Windows PowerShell 用 `;` 分隔）：

```powershell
cd "homework_week3/submit"

# 交付项 2：跑测试（结果与 pytest_result.txt 一致）
python -m pytest -q                       # → 101 passed

# 交付项 3/4/5：重新生成运行 / 审计 / 恢复记录到 demo_artifacts/
python run_demo.py                        # → ALL_DEMOS_OK
```

- 全程 **离线、确定性**：用 `FakeModel` 按脚本产出动作，不联网、不依赖真实 LLM，结果可复现。
- `run_test` 统一使用 `python -B -m pytest -q test_app.py`（`-B` 关闭字节码写入，规避陈旧 `.pyc` 竞态）。
- 环境：Python 3.11+，依赖 `pydantic` v2 与 `pytest`。

---

## 八、声明

- 本提交 **未修改 `homework_week3/smolagents/` 下任何文件**（18 个 `.py` 源码保持原样，仅只读引用）。
- 所有代码位于独立目录，模型只负责「决策」，程序负责「门禁、执行边界、状态、审计、恢复、最终验收」。
- 详细设计、工具目录、门禁/边界规则与完整验收对照表见 [`README.md`](README.md)。
