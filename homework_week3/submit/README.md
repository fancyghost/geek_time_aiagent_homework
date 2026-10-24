# Guarded Coding Harness（可恢复的受控编码执行体）

在 **不修改 smolagents 源码** 的前提下，为 `ToolCallingAgent` 增加一层「治理外壳」：
**模型只负责决策（想调用哪个工具、传什么参数），程序负责门禁、执行边界、状态、审计、恢复与最终验收。**
据此可安全、可复现地完成两类仓库任务：**代码理解（code_understanding）** 与 **测试修复（test_repair）**。

> 目录：`homework_week3/guarded_harness/`
> 上游源码：`homework_week3/smolagents/`（**全程只读引用，一字未改**）

---

## 1. 设计目标与核心原则

- **唯一入口门禁**：所有 Tool Call 都必须经过 `Gate.execute(GateRequest)`，被拒动作 **绝不进入 handler**。
- **哑 handler**：`catalog.py` 的 handler 只做机械副作用（读/写/列目录/跑命令）并产出证据，**不含任何业务/门禁规则**。
- **模型声称 ≠ 完成**：模型的 `final_answer` 只是「声称完成」，是否接受由 `CompletionChecker` 依据 **交付物 + 新鲜证据** 判定（H1/H2）。
- **可恢复**：每个安全步骤后 **原子落盘** Checkpoint（C1）；跨进程恢复预算不清零（C3）；结果未知的写 **禁止自动重放**（C2）；工作区变化使旧证据/审批失效（C4）。
- **全程可审计**：放行/拒绝/审批/执行/结果/状态变更都写入 **append-only** 审计日志，可按 `tool_call_id` 还原完整决策链（A1/A2）。
- **离线确定性**：测试与 Demo 用 `FakeModel` 按脚本产出动作，不联网、不依赖真实 LLM，结果 **完全可复现**。

---

## 2. 目录结构

```
guarded_harness/
├── _bootstrap.py         # 仅把本目录与 ../smolagents/src 加入 sys.path（不改源码）
├── audit.py              # append-only 审计日志（A1/A2）
├── state_store.py        # 运行状态 / 预算 / 证据 / 步骤 / 审批 / 幂等键（权威真相）
├── sandbox.py            # 执行边界：路径/软链/敏感目录/命令白名单/超时/环境/输出（S1-S4）
├── catalog.py            # 工具目录：严格参数模型 + 哑 handler + 证据类型
├── gate.py               # 唯一入口门禁（G1-G4）
├── completion.py         # 最终验收判定（H1-H3）
├── checkpoint.py         # 原子保存 / 恢复 / 对账（C1-C4）
├── coding_tools.py       # smolagents 适配层：GuardedTool / GuardedToolCallingAgent（T1/T2）
├── fake_model.py         # 确定性脚本模型（离线可复现）
├── harness.py            # 主循环编排：装配 + 驱动 ReAct + 预算/审批/恢复/验收
├── run_demo.py           # 一键跑通四个交付 Demo，产出 demo_artifacts/
├── pytest.ini            # testpaths=tests；norecursedirs 排除 smolagents
├── tests/                # 6 个测试文件 + conftest.py（101 用例）
│   ├── conftest.py
│   ├── test_sandbox.py           # S1-S4 + 输出/环境边界 + 指纹
│   ├── test_state_checkpoint.py  # 状态存储 + C1-C4
│   ├── test_gate.py              # G1-G4
│   ├── test_audit.py             # A1/A2 + append-only 语义
│   ├── test_completion.py        # H1-H3
│   └── test_harness_loop.py      # 端到端 T1/T2 + 集成路径
└── demo_artifacts/       # run_demo.py 产出的交付记录（见 §8）
```

**不修改 smolagents 的机制**：`_bootstrap.py` 在导入 smolagents **之前** 把 `../smolagents/src` 插入 `sys.path`，
使 `import smolagents` 指向上游未改动源码；集成层通过 **继承 + 重写 `process_tool_calls`** 接入，未触碰任何上游文件。

---

## 3. 架构与数据流

```
        FakeModel（脚本动作）
              │  ChatMessage(tool_calls=[...])
              ▼
   GuardedToolCallingAgent.process_tool_calls   ← 重写：注入 tool_call_id、收敛 observation
              │  GuardedTool.forward(**kwargs)
              ▼
        Gate.execute(GateRequest)   ← 唯一入口
   ┌───────────────────────────────────────────────┐
   │ 工具存在性 → G1 参数校验 → 沙箱前置(S1/S2/S3)   │
   │ → G2 重复拦截 → G4 幂等 → 写预算 → G3 审批       │
   │ → 放行：写意图先登记(C2) → handler → 证据/步骤   │
   └───────────────────────────────────────────────┘
              │  GateResult(ok / code / content)
              ▼
   StateStore（权威状态）→ Checkpoint.save（原子落盘 C1）→ AuditLog.append（A1/A2）
              │
              ▼
   harness._loop：预算兜底(H3) / 审批暂停(G3) / final → CompletionChecker.evaluate(H1/H2)
              │                                     ├─ 接受 → COMPLETED
              │                                     └─ 拒绝 → 缺口喂回模型继续
              ▼
        RunOutcome（结构化停机原因 + 完整运行记录）
```

主循环 **手动驱动** smolagents 的 `_step_stream`（而非 `agent.run`），以便精确掌控预算、审批暂停与 Checkpoint 落盘时机。

---

## 4. 工具目录（catalog.py）

| 工具 | 效果 / 风险 | 需审批 | 超时 | 路径/命令字段 | 产出证据 |
|------|-------------|:------:|:----:|---------------|----------|
| `read_file`  | READ / LOW      | 否 | 2s | `path` | `FILE_READ` |
| `list_dir`   | READ / LOW      | 否 | 2s | `path` | `LIST_DIR` |
| `write_file` | WRITE / HIGH    | **是** | 2s | `path` | `DELIVERABLE` |
| `run_test`   | SHELL / MEDIUM  | 否 | 5s | `command`（白名单） | `TEST_RESULT` |
| `final_answer` | READ / LOW    | 否 | 1s | — | 无（由 completion 判定） |

- 参数模型基于 pydantic v2 `StrictArgs(extra="forbid")`：**缺字段 / 多余字段 / 类型错误** 一律 G1 拒绝。
- `tool_call_id` 由系统注入，**模型不可见**，用于贯穿状态、审计与幂等。

---

## 5. 门禁与执行边界规则

**Gate（gate.py）拒绝码：**

| code | 触发条件 | 验收 |
|------|----------|:----:|
| `TOOL_NOT_FOUND` | 未知工具名 | G1 |
| `INVALID_ARGUMENT` | pydantic 严格校验失败（缺/多/类型错） | **G1** |
| `DUPLICATE_ACTION` | 同一 (工具,参数) 连续重复达阈值（默认 3） | **G2** |
| `WAITING_APPROVAL` | 写操作无有效审批 → 登记待审批并暂停 | **G3** |
| `NO_APPROVAL` | 以 USER 身份重放但仍无授权 | G3 |
| `ALREADY_EXECUTED` | 相同写动作已成功执行过 → 幂等拒绝重放 | **G4** |
| `WRITE_BUDGET_EXCEEDED` | 写入次数预算耗尽 | H3 |
| `ALLOWED` / `OK` | 放行 / handler 成功执行 | — |

**Sandbox（sandbox.py）越界码：**

| code | 触发条件 | 验收 |
|------|----------|:----:|
| `PATH_ESCAPE` | 相对/绝对路径逃逸出工作区（如 `../../.env`） | **S1** |
| `SENSITIVE_PATH` | 命中敏感目录/模式（按 **软链接真实落点** 校验） | **S2** |
| `COMMAND_NOT_ALLOWED` | 命令不在白名单内 | **S3** |
| `DANGEROUS_COMMAND` | 命中危险命令模式（如 `rm -rf`、`os.system`） | S3 |
| `PATH_NOT_FOUND` / `PATH_INVALID` / `COMMAND_INVALID` | 路径/命令非法 | — |

- **S4 超时**：`run_test` 超时 **真实终止进程组**（Windows `taskkill /F /T`，POSIX `os.killpg`），而非只停止等待。
- **输出边界**：截断超长输出、脱敏命中的敏感串。
- **环境边界**：子进程只透传白名单变量 `PATH / PYTHONPATH / PYTHONUTF8 / SYSTEMROOT / PATHEXT / HOME / LANG / LC_ALL`，避免密钥泄漏。

---

## 6. 状态 / 恢复 / 审计 / 验收

- **状态（state_store.py）**：`RunState` 为唯一权威真相，含 `Budget`（默认 `max_steps=12 / max_writes=8 / max_seconds=600`）、
  证据、步骤记录、审批、`args_digest`（内容寻址幂等键，与参数顺序无关）。
- **恢复（checkpoint.py）**：
  - **C1** `mkstemp + fsync + os.replace` 原子写，任何时刻磁盘上都是完整状态，绝无半截文件；
  - **C2** 恢复时对账「写意图已登记但结果未知（PENDING）」→ `unknown_result_tool_calls`，上层 **禁止自动重放**；
  - **C3** `load / load_latest` 无损读回（含预算计数），跨实例恢复 **不清零**；
  - **C4** 当前工作区指纹与保存时不一致 → **旧证据失效、未用审批吊销**，模型须在当前版本重新取证。
- **审计（audit.py）**：**A1** 被拒动作必须留痕（含发起者/阶段/规则）；**A2** 完整决策链可按 `tool_call_id` 还原；
  记录为 `frozen dataclass`（不可变）、`seq` 单调递增、可选 JSONL 跨进程追加。
- **验收（completion.py）**：**H1** 答案非空且交付物/证据齐备；**H2** 证据须绑定「当前工作区指纹」（源码一改，旧测试证据即失效，须重跑）；
  **H3** 预算/最大轮数由主循环兜底，判定确定性、无副作用。

---

## 7. 与 smolagents 的集成（T1/T2）

- **T1（不改源码接入）**：`GuardedTool(Tool)` 以 `skip_forward_signature_validation=True` + 动态 `inputs` 承载受控参数；
  `GuardedToolCallingAgent(ToolCallingAgent)` 重写 `process_tool_calls`，把每个工具调用导向 `Gate`。未修改任何 smolagents 文件。
- **T2（tool_call_id 对齐）**：系统注入的 `tool_call_id` 贯穿 `ToolCall → GateRequest → StepRecord → AuditRecord`，
  使「模型的第 N 次调用」与「审计链、状态步骤、幂等键」严格一一对应。

---

## 8. 如何运行

在 `guarded_harness/` 目录下执行（Windows PowerShell 用 `;` 分隔）：

```powershell
# 1) 全量测试（101 用例，离线确定性）
cd "homework_week3/guarded_harness"
python -m pytest -q

# 2) 四个交付 Demo（产出 demo_artifacts/）
python run_demo.py
```

**pytest 实际结果：**

```
101 passed in 4.73s
```

> `run_test` 统一使用 `python -B -m pytest -q test_app.py`：`-B` 关闭字节码写入，
> 规避「同秒覆写源码后子进程加载陈旧 `.pyc`」的竞态。

---

## 9. 交付记录（demo_artifacts/）

`python run_demo.py` 跑通四个 Demo 并写入以下记录（均为 UTF-8）：

| 文件 | 内容 | 对应交付 |
|------|------|----------|
| `run_record.json` | 一次测试修复的 **完整运行记录**（运行/预算/证据/步骤/审批/12 条审计链/恢复对账） | 「至少一条完整运行记录」 |
| `audit_chain.txt` | 按 `tool_call_id` 渲染的 **可读决策链**（拒绝 / 读放行 / 写：待审批→批准→执行） | 「一份审计记录」 |
| `audit.jsonl` | **append-only** 原始审计日志（11 行） | 「一份审计记录」 |
| `checkpoint_recovery.json` | **Checkpoint 恢复记录**（C3 跨实例恢复预算不清零 + C2 结果未知禁止重放） | 「一份 Checkpoint 恢复记录」 |
| `approval_flow.json` | 人工审批流水（G3 暂停 → 批准 → 重放执行 → 完成） | 审批链路佐证 |
| `summary.json` | 四个 Demo 的结构化结果汇总 | 复现佐证 |

**Demo 一览：**

1. **完整运行**：代码理解（`read_file → final`，2 步接受）+ 测试修复（预授权 `write_file → run_test 通过 → final`，4 步、1 次写，接受）。
2. **审计链**：`write_file` 缺 `content` → G1 拒绝留痕（A1）；`read_file` 放行；未授权 `write_file` → 暂停（A2 confirm）→ 批准 → 执行（A2 完整链）。
3. **Checkpoint 恢复**：实例 1 跑满会话步数上限（`max_steps`，2 步）→ 实例 2 从同一 Checkpoint `resume`（`completed`，**steps_used=3** 未清零）；另构造「写结果未知」快照 → `resume` 判为 `unknown_result` 且 **不重放**。
4. **人工审批**：未授权写入 → `waiting_approval`（暂停期间工作区 **未改动**）→ `approve_pending` → 经 Gate 重放写入 → 跑测试 → `completed`（4 步、1 次写）。

---

## 10. 完整运行记录示例（run_record.json 摘录）

```jsonc
{
  "run":    { "run_id": "demo1-repair", "task_kind": "test_repair",
              "status": "completed", "stop_reason": "completed",
              "final_answer": "已将 add 修复为 a + b，test_app.py 通过。" },
  "budget": { "max_steps": 12, "steps_used": 4, "writes_used": 1, "seconds_used": 0.472 },
  "evidence": [
    { "kind": "file_read",   "ref": "test_app.py", "fingerprint": "d2239ab1…" },
    { "kind": "deliverable", "ref": "app.py",      "fingerprint": "bdf8e1a4…" },
    { "kind": "test_result", "ref": "python -B -m pytest -q test_app.py",
      "detail": { "returncode": 0, "passed": true, "stdout_tail": "1 passed in 0.01s" },
      "fingerprint": "bdf8e1a4…" }        // ← 与交付物同指纹，证明「当前版本通过」（H2）
  ],
  "steps":  [ /* read_file → write_file → run_test → final_answer，各带 tool_call_id / args_digest / decision */ ],
  "approvals": [ { "approval_id": "ap-demo1", "tool_name": "write_file", "consumed": true } ],
  "audit_chain": [ /* 12 条：APPROVAL_GRANTED, RUN_STARTED, gate/result×4, RUN_FINISHED */ ],
  "recovery": null
}
```

## 11. 审计链示例（audit_chain.txt 摘录）

```
### A1 · 缺参数写被 G1 拒绝 (tool_call_id=tc-bad)
#  2 [gate/deny] rule=INVALID_ARGUMENT actor=model tool=write_file :: 参数校验失败

### A2 · 读放行 (tool_call_id=tc-read)
#  3 [gate/allow]     rule=ALLOWED actor=model  tool=read_file :: 全部前置检查通过
#  4 [result/executed] rule=OK     actor=harness tool=read_file :: handler executed

### A2 · 写：待审批 → 批准 → 执行 (tool_call_id=tc-write)
#  5 [approval/confirm]  rule=WAITING_APPROVAL actor=model :: 写操作等待人工审批：app.py
#  8 [gate/allow]        rule=ALLOWED  actor=user :: 全部前置检查通过
#  9 [approval/approved] rule=APPROVED actor=user :: 审批 ap-demo2 已消费
# 10 [result/executed]   rule=OK       actor=harness :: handler executed
```

## 12. Checkpoint 恢复示例（checkpoint_recovery.json 摘录）

```jsonc
{
  "c3_cross_instance_resume": {
    "instance_1_partial": { "stop_reason": "max_steps",  "steps_used": 2, "accepted": false },
    "instance_2_resumed": { "stop_reason": "completed",  "steps_used": 3, "accepted": true, "resumed": true },
    "recovery_report":    { "resumed_from_step": 2, "workspace_changed": false }
  },
  "c2_unknown_result_no_replay": {
    "outcome":         { "stop_reason": "unknown_result", "writes_used": 0, "accepted": false },
    "recovery_report": { "unknown_result_tool_calls": ["c2-write"] }   // ← 禁止自动重放
  }
}
```

---

## 13. 验收标准对照

| 编号 | 要求 | 实现位置 | 测试 |
|:----:|------|----------|------|
| G1 | 参数严格校验，非法调用不进 handler | `gate.py` `INVALID_ARGUMENT` | `test_gate.py` |
| G2 | 连续重复动作拦截 | `gate.py` `DUPLICATE_ACTION` | `test_gate.py` |
| G3 | 写操作需人工审批（暂停/批准/重放） | `gate.py` + `harness.approve_pending` | `test_gate.py` / `test_harness_loop.py` |
| G4 | 幂等：重复写拒绝重放 | `gate.py` `ALREADY_EXECUTED` | `test_gate.py` / `test_harness_loop.py` |
| S1 | 路径逃逸拒绝 | `sandbox.py` `PATH_ESCAPE` | `test_sandbox.py` |
| S2 | 软链接/敏感目录按真实落点拒绝 | `sandbox.py` `SENSITIVE_PATH` | `test_sandbox.py` |
| S3 | 命令白名单 / 危险命令拒绝 | `sandbox.py` `COMMAND_NOT_ALLOWED` / `DANGEROUS_COMMAND` | `test_sandbox.py` |
| S4 | 超时真实终止进程组 | `sandbox.py` `run_command` | `test_sandbox.py` |
| C1 | 原子保存无半截文件 | `checkpoint.py` `_atomic_write` | `test_state_checkpoint.py` |
| C2 | 结果未知的写禁止自动重放 | `checkpoint.reconcile` + `harness.resume` | `test_state_checkpoint.py` / `test_harness_loop.py` |
| C3 | 跨实例恢复预算不清零 | `checkpoint.load` + `harness.resume` | `test_harness_loop.py` |
| C4 | 工作区变化使证据/审批失效 | `checkpoint.reconcile` | `test_state_checkpoint.py` |
| A1 | 被拒动作留痕 | `audit.py` + `gate.py` | `test_audit.py` |
| A2 | 完整决策链可还原 | `audit.chain` / `render_chain` | `test_audit.py` |
| H1 | 交付物/证据齐备才接受 | `completion.py` | `test_completion.py` |
| H2 | 证据新鲜度（绑定当前指纹） | `completion.py` | `test_completion.py` |
| H3 | 预算/最大轮数兜底 | `harness._loop` | `test_harness_loop.py` |
| T1 | 不改 smolagents 接入 | `coding_tools.py` / `_bootstrap.py` | `test_harness_loop.py` |
| T2 | tool_call_id 贯穿审计 | `coding_tools.py` / `gate.py` | `test_harness_loop.py` |

---

## 14. 环境依赖

- Python 3.11+（用到 `StrEnum`、`slots=True`、`X | None` 语法）。
- `pydantic` v2、`pytest`；smolagents 依赖随上游源码（`../smolagents/src`）只读引用。
- 离线运行：不联网、不调用真实 LLM。

---

## 15. 声明

本作业 **未修改 `homework_week3/smolagents/` 下任何文件**（18 个 `.py` 源码保持原样）；
所有新增代码位于独立目录 `homework_week3/guarded_harness/`，仅通过 `_bootstrap.py` 以 `sys.path` 方式只读引用上游源码。
