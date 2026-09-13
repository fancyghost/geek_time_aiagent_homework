# 转账（transfer）工具治理演示 —— 使用方法与测试结果

> 模块：`homework_week2/tool_governance.py`　测试：`tests/test_tool_governance.py`
> 环境：Python 3.12 + Pydantic v2 + pytest（项目未安装 pytest-asyncio，测试用同步函数包裹 `asyncio.run` 驱动异步场景）

## 一、概述

`tool_governance.py` 用一个**固定优先级的三态权限状态机**（allow / deny / confirm）治理所有工具调用。本轮为高风险写操作新增了 **transfer（转账）** 工具，完整串起：

严格 Schema 校验 → 业务预检 → 参数绑定审批 → 非幂等写超时不重试 → 结果脱敏 → 审计。

核心不变量：

- `PermissionEngine.decide` 的 9 步优先级顺序是固定框架，未改动。
- 所有调用必须经过 `ToolRuntime.invoke`，不直接调 `transfer_handler`。
- `TransferArgs` 继承 `StrictArgs`（`extra="forbid"`、`strict=True`），拒绝模型注入的额外参数（如自造 `approved`）。

## 二、使用方法

### 2.1 离线治理演示（不依赖大模型）

```bash
python homework_week2/tool_governance.py
```

不加 `--agent` 即走 `run_offline_demo()`，演示 `get_order / create_refund / run_shell` 在 allow / confirm / deny 以及 plan / bypass 模式下的行为。

> 注：当前 `run_offline_demo()` 为原版，**未包含 transfer**；transfer 的演示见 §2.4 与 §四。

### 2.2 真实模型 Agent Loop（本地 llama.cpp）

```bash
python homework_week2/tool_governance.py --agent --input "请查询订单 ord_1001 的状态和可退金额"
```

- 需本地 llama.cpp 服务监听 `http://127.0.0.1:8080/v1`；
- 模型名取环境变量 `DEEPSEEK_MODEL`（默认 `deepseek-v4-flash`）；
- 模型发起的每次工具调用仍经过同一个 `ToolRuntime.invoke` 治理。

### 2.3 运行 transfer 治理测试

```bash
# 项目根目录
python -m pytest tests/test_tool_governance.py -v -k "transfer"

# 或直接运行
python tests/test_tool_governance.py
```

### 2.4 单独演示 transfer 的「审批前 CONFIRM + 结果脱敏」（不改任何源码）

用模块现成的公开 API 从外部驱动整条管线（可存成临时脚本，或压成 `python -c` 一行执行）：

```python
import sys, asyncio
sys.path.insert(0, "homework_week2")
import tool_governance as tg

tg.reset_side_effects()
runtime, approvals, audit = tg.build_runtime()
ctx = tg.base_context(
    permissions=frozenset({"transfer:execute"}),
    allowed_tools=frozenset({"transfer"}),
)
args = {"from_account": "ACC-A-123456", "to_account": "ACC-A-654321", "amount": 100.0}

# 审批前 -> CONFIRM（handler 不执行）
r0 = asyncio.run(runtime.invoke(tg.ToolCall("call_no_approval", "transfer", args), ctx))
print("[1] BEFORE_APPROVAL ->", r0.action, "/", r0.code)

# 审批后 -> 放行，结果 content 中账号被脱敏
approvals.approve("ap", ctx, "transfer", args)
ctx2 = tg.base_context(permissions=frozenset({"transfer:execute"}),
                       allowed_tools=frozenset({"transfer"}), approval_id="ap")
r1 = asyncio.run(runtime.invoke(tg.ToolCall("call_approved", "transfer", args), ctx2))
print("[2] AFTER_APPROVAL ->", r1.action, "/", r1.code)
print("[2] RESULT_CONTENT ->", r1.content)

# 审计日志（只记参数名 argument_keys，不记参数值）
for rec in audit.records:
    print("    ", rec.tool_name, rec.phase, rec.decision, rec.code, list(rec.argument_keys))
```

## 三、transfer 工具治理规格

| 项 | 配置 |
|---|---|
| 参数模型 `TransferArgs` | `from_account` / `to_account`：正则 `^ACC-[A-Z]-[0-9]{6}$`；`amount`：`>0` 且 `≤100000`；继承 `StrictArgs`（`extra="forbid"`、`strict=True`） |
| 策略 `ToolPolicy` | `effect=WRITE`、`risk=HIGH`、`permission=transfer:execute`、`requires_approval=True`、`timeout_seconds=1.5`、`max_retries=0`、`idempotent=False` |
| 业务预检 `transfer_precheck` | ① `amount > 50000` → `EXCEED_LIMIT`；② 转出账户余额 `< amount` → `INSUFFICIENT_BALANCE` |
| 处理函数 `transfer_handler` | `amount > 80000` 先 `await asyncio.sleep(3.0)` 制造超时（位于改余额之前）；转入账户不存在 → `ACCOUNT_NOT_FOUND`；扣转出、加转入；返回 `txn_id / from / to / amount / status` |
| `canonical_target` | `f"{from_account}->{to_account}"` |

预设账户 `ACCOUNTS`（Key = `(tenant_id, account_id)`，Value = 余额）：

| 租户 | 账户 | 余额 |
|---|---|---|
| tenant_a | ACC-A-123456 | 100000.0 |
| tenant_a | ACC-A-654321 | 5000.0 |
| tenant_a | ACC-A-888888 | 20000.0 |
| tenant_b | ACC-B-111111 | 50000.0 |

## 四、测试结果

### 4.1 pytest：5 个用例全部 PASSED

```text
tests/test_tool_governance.py::test_transfer_schema_rejects_extra PASSED [ 20%]
tests/test_tool_governance.py::test_transfer_precheck_insufficient PASSED [ 40%]
tests/test_tool_governance.py::test_transfer_precheck_exceed_limit PASSED [ 60%]
tests/test_tool_governance.py::test_transfer_approval_binding PASSED     [ 80%]
tests/test_tool_governance.py::test_transfer_timeout_no_retry PASSED     [100%]
============================== 5 passed in 1.59s ==============================
```

| # | 测试 | 场景 | 期望结果 |
|---|---|---|---|
| 1 | `test_transfer_schema_rejects_extra` | 非法账号 / 多传 `approved` 字段 | `INVALID_ARGUMENT`，且 `transfer_executions == 0` |
| 2 | `test_transfer_precheck_insufficient` | 余额 5000 转账 6000 | `INSUFFICIENT_BALANCE`，副作用 0 |
| 3 | `test_transfer_precheck_exceed_limit` | 转账 60000（超 5 万限额） | `EXCEED_LIMIT`，副作用 0 |
| 4 | `test_transfer_approval_binding` | 审批金额 100、执行改成 200 | `action=CONFIRM` + `APPROVAL_REQUIRED`，副作用 0 |
| 5 | `test_transfer_timeout_no_retry` | 转账 90000 触发超时 | `TIMEOUT_UNKNOWN`，且 `transfer_executions <= 1` |

> **关于测试 5**：`transfer_precheck` 在 5 万处拦截，9 万在正常流程根本到不了 handler，因此该用例用 `dataclasses.replace(t, precheck=None)` 单独构造去掉 precheck 的 transfer 工具来隔离验证——**仍然经过 `runtime.invoke` 与完整的 `decide` / 审批 / 超时管线**，不直接调 handler，用以验证「非幂等写操作超时 → `TIMEOUT_UNKNOWN` 且 `max_retries=0` 不自动重试」。

### 4.2 端到端演示输出（审批前 CONFIRM + 结果脱敏）

```text
[1] BEFORE_APPROVAL -> confirm / APPROVAL_REQUIRED
[2] AFTER_APPROVAL -> allow / OK
[2] RESULT_CONTENT -> {'txn_id': 'proved', 'from': 'ACC-A-****3456', 'to': 'ACC-A-****4321', 'amount': 100.0, 'status': 'completed'}
[3] AUDIT_LOG (argument_keys only):
     transfer decision confirm APPROVAL_REQUIRED keys= ['amount', 'from_account', 'to_account']
     transfer decision allow APPROVED keys= ['amount', 'from_account', 'to_account']
     transfer execution executed OK keys= ['amount', 'from_account', 'to_account']
[4] transfer_executions= 1
```

### 4.3 关键结论

- **审批前 CONFIRM**：未拿到参数绑定审批时，高风险写操作返回 `action=confirm / code=APPROVAL_REQUIRED`，`handler` 不执行（`transfer_executions` 不增）。
- **结果脱敏**：审批通过放行后，返回给模型的结果 `content` 中账号被脱敏为 `ACC-A-****3456` / `ACC-A-****4321`。
- **审计隐私**：`AuditRecord` 只记录 `argument_keys`（参数名），**不记录账号等参数值**；脱敏账号只出现在工具结果 `content` 中，而非审计日志。
- **非幂等写超时**：`WRITE + idempotent=False` 超时映射为 `TIMEOUT_UNKNOWN`（结果状态未知），且 `max_retries=0` 绝不自动重试，最多执行一次。

## 五、脱敏规则（`_redact`）

- 邮箱：`alice@example.com` → `***@***`
- 账号：`ACC-A-123456` → `ACC-A-****3456`（保留 `ACC-X-` 前缀与末 4 位，中间固定 4 个星号）
- 键名命中 `token / secret / password / authorization` 的字段 → 值整体替换为 `***`

## 六、备注

- 仓库中存在两份 `test_tool_governance.py`（`homework_week2/` 与 `tests/`），内容一致；**运行以 `tests/` 那份为准**。若执行 `pytest -k transfer` 不带路径，会同时收集两份导致用例数翻倍，建议始终指定路径 `tests/test_tool_governance.py`。
- Windows PowerShell 控制台默认 GBK 编码，直接 `print` 中文可能出现乱码；本文档演示输出均以 ASCII 关键字段（`confirm` / `APPROVAL_REQUIRED` / `ACC-A-****3456`）为准，不受影响。
