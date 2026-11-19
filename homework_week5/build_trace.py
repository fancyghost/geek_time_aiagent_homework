"""生成 Agent 对话轨迹示例数据（仅用 Python 标准库，零第三方依赖）。

产出：
  data/traces.js   —— window.TRACES = [...]，供 index.html 以 <script> 直接加载，
                      双击打开（file://）即可运行，无需本地服务器、无 CORS 问题。
  data/traces.json —— 同一份数据的纯 JSON，便于二次分析 / 导入其它工具。

数据真实性：
  轨迹以本项目 db_agent 的真实要素为骨架——真实模型名（llama-local / deepseek-v4-flash）、
  真实提示词模板（db_query@v1 / db_assistant@v2 / general_chat@v1）、真实工具
  （search_orders / transfer）、真实治理概念（order:read 权限预检、转账人工审批、限流重试）。
  若仓库存在 logs/observability.db，则读取其真实延迟分位数作为「时长量级校准」写入 meta，
  使示例调用时长落在项目实测区间内（约 0.2s ~ 6.6s）。

运行：
  python build_trace.py            # 生成 3 条示例对话轨迹
  python build_trace.py --stats    # 额外打印 observability.db 的真实延迟统计
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
DATA_DIR = BASE_DIR / "data"
OBS_DB = REPO_ROOT / "logs" / "observability.db"

STARTED_AT = "2026-11-19T09:12:00+08:00"


# --------------------------------------------------------------------------- #
# 真实延迟校准：从项目 observability.db 读取分位数（只读，缺失则优雅降级）
# --------------------------------------------------------------------------- #
def read_calibration() -> dict:
    if not OBS_DB.exists():
        return {"source": "synthetic", "note": "未找到 observability.db，使用代表性时长"}
    try:
        conn = sqlite3.connect(f"file:{OBS_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = [r["latency_ms"] for r in conn.execute(
            "SELECT latency_ms FROM call_records WHERE latency_ms IS NOT NULL")]
        models = [dict(r) for r in conn.execute(
            "SELECT model, COUNT(*) AS calls FROM call_records GROUP BY model")]
        conn.close()
    except Exception as exc:  # noqa: BLE001
        return {"source": "synthetic", "note": f"读取 observability.db 失败：{exc}"}
    if not rows:
        return {"source": "synthetic", "note": "observability.db 无延迟记录"}
    ordered = sorted(rows)

    def pct(p: float) -> float:
        return round(ordered[min(len(ordered) - 1, int(len(ordered) * p))], 1)

    return {
        "source": str(OBS_DB.relative_to(REPO_ROOT)),
        "samples": len(ordered),
        "latency_ms": {"min": round(ordered[0], 1), "p50": pct(0.5),
                       "p95": pct(0.95), "max": round(ordered[-1], 1)},
        "models": models,
    }


# --------------------------------------------------------------------------- #
# 构造辅助
# --------------------------------------------------------------------------- #
def call(cid, parent, turn, ctype, name, start, dur, *, status="ok", model=None,
         template=None, tool=None, ttft=None, retries=0, error_code=None,
         error_message=None, tokens=None, summary="", content=None) -> dict:
    return {
        "id": cid, "parent_id": parent, "turn": turn, "type": ctype, "name": name,
        "model": model, "template_ref": template, "tool": tool, "status": status,
        "start_ms": start, "duration_ms": dur, "ttft_ms": ttft, "retries": retries,
        "error_code": error_code, "error_message": error_message,
        "tokens": tokens or {"input": 0, "output": 0, "cached": 0, "reasoning": 0},
        "summary": summary, "content": content or {},
    }


def llm_tokens(inp, out, *, cached=0, reasoning=0) -> dict:
    return {"input": inp, "output": out, "cached": cached, "reasoning": reasoning}


def finalize(trace: dict) -> dict:
    calls = trace["calls"]
    total = max(c["start_ms"] + c["duration_ms"] for c in calls)
    trace["total_ms"] = total
    trace["turn_count"] = len(trace["turns"])
    trace["call_count"] = len(calls)
    trace["error_count"] = sum(1 for c in calls if c["status"] == "error")
    trace["started_at"] = STARTED_AT
    return trace


# --------------------------------------------------------------------------- #
# 轨迹 1：订单查询 → 取消 → 转账（含权限预检 / 人工审批 / 限流重试）
# --------------------------------------------------------------------------- #
def trace_orders_transfer() -> dict:
    turns = [
        {"index": 1, "start_ms": 0,
         "user": "帮我查一下我昨天未支付的订单。",
         "assistant": "您昨天有 1 笔未支付订单：ORD-1001（机械键盘，¥129.00）。需要我帮您取消吗？"},
        {"index": 2, "start_ms": 4600,
         "user": "取消它，然后给 user_other 转 50 元。",
         "assistant": "已取消订单 ORD-1001；向 user_other 转账 ¥50.00 成功（交易号 TX-20261119-0042）。首次转账触发限流，已自动重试 1 次后成功。"},
        {"index": 3, "start_ms": 13000,
         "user": "这次操作一共花了多久？做个总结。",
         "assistant": "本次会话共 3 轮、12 次调用，端到端约 15.0 秒。其中人工审批等待 3.2 秒占比最高；转账首次限流失败后重试成功；订单查询与取消均一次通过。"},
    ]
    calls = [
        call("c1", None, 1, "llm", "意图识别与工具规划", 120, 1610,
             model="llama-local", template="db_query@v1", ttft=410,
             tokens=llm_tokens(312, 86, reasoning=40),
             summary="解析「昨天/未支付」→ 决定调用 search_orders",
             content={"input": "system: 你是 db_agent 数据库助手…\nuser: 帮我查一下我昨天未支付的订单。",
                      "output": "tool_call: search_orders(status=\"unpaid\", created_on=\"yesterday\")",
                      "reasoning": "用户要查昨天未支付订单，需调用 search_orders 并传 status=unpaid、created_on=yesterday。"}),
        call("c2", "c1", 1, "governance", "权限预检 order:read", 1762, 14,
             tool="search_orders", summary="校验 ctx.permissions 含 order:read → 通过",
             content={"input": "required_permission=order:read; user=user_demo",
                      "result": {"allowed": True, "permission": "order:read"}}),
        call("c3", "c1", 1, "tool", "search_orders", 1792, 132,
             tool="search_orders", summary="返回 1 笔未支付订单 ORD-1001",
             content={"args": {"status": "unpaid", "created_on": "yesterday"},
                      "result": [{"order_id": "ORD-1001", "user_id": "user_demo",
                                  "status": "unpaid", "amount": 129.0,
                                  "items": ["机械键盘"], "created_at": "2026-11-18"}]}),
        call("c4", "c1", 1, "llm", "组织自然语言回答", 1962, 2380,
             model="llama-local", template="db_assistant@v2", ttft=520,
             tokens=llm_tokens(486, 142, cached=120),
             summary="把订单结果整理成中文回答",
             content={"input": "tool_result: [{ORD-1001, 机械键盘, 129.0, unpaid}] …",
                      "output": "您昨天有 1 笔未支付订单：ORD-1001（机械键盘，¥129.00）。需要我帮您取消吗？"}),
        call("c5", None, 2, "llm", "解析复合指令并规划", 4720, 1180,
             model="deepseek-v4-flash", template="db_query@v1", ttft=260,
             tokens=llm_tokens(642, 118, reasoning=64),
             summary="拆成 cancel_order + transfer 两个工具调用",
             content={"input": "user: 取消它，然后给 user_other 转 50 元。",
                      "output": "tool_calls: [cancel_order(order_id=ORD-1001), transfer(to=user_other, amount=50)]"}),
        call("c6", "c5", 2, "tool", "cancel_order", 5942, 96,
             tool="cancel_order", summary="ORD-1001 状态 unpaid → cancelled",
             content={"args": {"order_id": "ORD-1001"},
                      "result": {"order_id": "ORD-1001", "status": "cancelled", "ok": True}}),
        call("c7", "c5", 2, "governance", "转账风控预检", 6082, 22,
             tool="transfer", status="pending", summary="敏感操作 → 需人工审批",
             content={"input": "transfer(to=user_other, amount=50); risk_score=0.62",
                      "result": {"allowed": False, "reason": "需人工审批", "approval_required": True}}),
        call("c8", "c7", 2, "approval", "人工审批（敏感转账）", 6122, 3200,
             tool="transfer", summary="审批人确认 → 通过（等待 3.2s）",
             content={"input": "审批请求：向 user_other 转账 ¥50.00，风险分 0.62",
                      "result": {"approved": True, "approver": "ops_admin", "wait_ms": 3200}}),
        call("c9", "c5", 2, "tool", "transfer（首次）", 9362, 210,
             tool="transfer", status="error", retries=0,
             error_code="RATE_LIMITED", error_message="触发限流 RATE_LIMIT_RPS=2.0，请稍后重试",
             summary="首次转账被限流拒绝",
             content={"args": {"to": "user_other", "amount": 50.0},
                      "error": {"code": "RATE_LIMITED", "message": "rate limit exceeded (2.0 rps)"}}),
        call("c10", "c5", 2, "tool", "transfer（重试 #1）", 9722, 240,
             tool="transfer", retries=1, summary="退避后重试成功，生成交易号",
             content={"args": {"to": "user_other", "amount": 50.0},
                      "result": {"tx_id": "TX-20261119-0042", "to": "user_other",
                                 "amount": 50.0, "status": "success"}}),
        call("c11", "c5", 2, "llm", "汇总操作结果", 10022, 2620,
             model="llama-local", template="db_assistant@v2", ttft=480,
             tokens=llm_tokens(724, 168, cached=210, reasoning=52),
             summary="合并取消/审批/重试结果成回答",
             content={"input": "tool_results: cancel_order ok; approval approved; transfer retry#1 success …",
                      "output": "已取消订单 ORD-1001；向 user_other 转账 ¥50.00 成功（交易号 TX-20261119-0042）。首次转账触发限流，已自动重试 1 次后成功。"}),
        call("c12", None, 3, "llm", "会话总结", 13122, 1840,
             model="llama-local", template="general_chat@v1", ttft=360,
             tokens=llm_tokens(910, 204, cached=320),
             summary="统计轮次/调用/耗时并总结",
             content={"input": "user: 这次操作一共花了多久？做个总结。",
                      "output": "本次会话共 3 轮、12 次调用，端到端约 15.0 秒…"}),
    ]
    return finalize({
        "id": "orders-transfer", "title": "订单查询 → 取消 → 转账",
        "subtitle": "含权限预检 · 人工审批 · 限流重试的完整 db_agent 会话",
        "agent": "db_agent", "harness": "Qoder Agent Harness",
        "turns": turns, "calls": calls,
        "meta": {"tools": ["search_orders", "cancel_order", "transfer"],
                 "models": ["llama-local", "deepseek-v4-flash"],
                 "highlights": ["人工审批等待 3.2s", "转账限流失败 → 重试成功", "3 轮 12 调用"]},
    })


# --------------------------------------------------------------------------- #
# 轨迹 2：RAG 知识问答（向量 + BM25 + RRF 混合检索，呼应 q2）
# --------------------------------------------------------------------------- #
def trace_rag_qa() -> dict:
    turns = [
        {"index": 1, "start_ms": 0,
         "user": "登录会话的有效期是多少分钟？",
         "assistant": "登录会话的有效期是 30 分钟，采用滑动过期；期间有受保护请求会把过期时间重置为「当前时间 + 30 分钟」[1][3]。"},
    ]
    calls = [
        call("r1", None, 1, "llm", "问题理解与检索规划", 90, 980,
             model="llama-local", template="db_query@v1", ttft=240,
             tokens=llm_tokens(188, 42, reasoning=24),
             summary="判定为知识问答 → 触发混合检索",
             content={"input": "user: 登录会话的有效期是多少分钟？",
                      "output": "retrieve(query=登录会话 有效期, mode=hybrid)"}),
        call("r2", "r1", 1, "retrieval", "向量检索 bge-large-zh-v1.5", 1110, 186,
             tool="vector_search", summary="语义召回 Top5（dim=1024）",
             content={"args": {"query": "登录会话的有效期是多少分钟？", "top_k": 5},
                      "result": [{"source": "登录与会话策略.md", "score": 0.5819},
                                 {"source": "常见问题FAQ.md", "score": 0.5179}]}),
        call("r3", "r1", 1, "retrieval", "BM25 关键词检索（jieba）", 1110, 44,
             tool="bm25_search", summary="词面召回 Top5（与向量并行）",
             content={"args": {"query": "登录 会话 有效期 分钟", "top_k": 5},
                      "result": [{"source": "登录与会话策略.md", "score": 2.5776},
                                 {"source": "常见问题FAQ.md", "score": 1.8284}]}),
        call("r4", "r1", 1, "retrieval", "RRF 倒数排名融合", 1310, 18,
             tool="rrf_fusion", summary="融合两路 → 证据 [1][3]",
             content={"args": {"mode": "reciprocal_rerank", "num_queries": 1},
                      "result": [{"rank": 1, "source": "登录与会话策略.md"},
                                 {"rank": 3, "source": "常见问题FAQ.md"}]}),
        call("r5", "r1", 1, "llm", "生成带来源引用的回答", 1360, 2410,
             model="llama-local", template="db_assistant@v2", ttft=540,
             tokens=llm_tokens(842, 156, cached=260, reasoning=48),
             summary="依据证据逐句标注 [n] 来源",
             content={"input": "evidence: [1] 登录与会话策略.md … [3] 常见问题FAQ.md …",
                      "output": "登录会话的有效期是 30 分钟，采用滑动过期…[1][3]。"}),
    ]
    return finalize({
        "id": "rag-qa", "title": "RAG 知识问答 · 混合检索",
        "subtitle": "向量 + BM25 并行召回 → RRF 融合 → 带来源回答",
        "agent": "db_agent", "harness": "LlamaIndex Hybrid RAG",
        "turns": turns, "calls": calls,
        "meta": {"tools": ["vector_search", "bm25_search", "rrf_fusion"],
                 "models": ["llama-local"],
                 "highlights": ["向量/BM25 并行检索", "RRF 融合", "回答带 [1][3] 来源"]},
    })


# --------------------------------------------------------------------------- #
# 轨迹 3：风控拦截 · 转账被拒（错误态密集，展示 danger / 拒绝路径）
# --------------------------------------------------------------------------- #
def trace_risk_block() -> dict:
    turns = [
        {"index": 1, "start_ms": 0,
         "user": "给陌生账号 user_unknown 转 10000 元。",
         "assistant": "抱歉，该转账被风控拦截：收款方为高风险陌生账号且金额超出免审额度，人工审批已拒绝。为保护您的资金安全，本次操作未执行。如需继续，请走线下大额转账审核流程。"},
    ]
    calls = [
        call("b1", None, 1, "llm", "指令解析", 100, 1120,
             model="deepseek-v4-flash", template="db_query@v1", ttft=250,
             tokens=llm_tokens(206, 58, reasoning=30),
             summary="识别大额转账 → 提交风控",
             content={"input": "user: 给陌生账号 user_unknown 转 10000 元。",
                      "output": "tool_call: transfer(to=user_unknown, amount=10000)"}),
        call("b2", "b1", 1, "governance", "风控预检（大额/陌生收款方）", 1260, 36,
             tool="transfer", status="error", error_code="RISK_BLOCKED",
             error_message="risk_score=0.93 超阈值；收款方命中黑名单",
             summary="风险分 0.93 → 拦截，转人工审批",
             content={"input": "transfer(to=user_unknown, amount=10000); risk_score=0.93",
                      "error": {"code": "RISK_BLOCKED", "message": "high risk payee + amount over limit"}}),
        call("b3", "b2", 1, "approval", "人工审批（大额复核）", 1320, 4100,
             tool="transfer", status="error", error_code="APPROVAL_REJECTED",
             error_message="审批人拒绝：收款方风险过高",
             summary="审批人拒绝（等待 4.1s）",
             content={"input": "审批请求：向 user_unknown 转账 ¥10000.00，风险分 0.93",
                      "result": {"approved": False, "approver": "risk_admin",
                                 "reason": "收款方命中黑名单，拒绝"}}),
        call("b4", "b1", 1, "llm", "解释拒绝原因并给出建议", 5480, 2280,
             model="llama-local", template="db_assistant@v2", ttft=470,
             tokens=llm_tokens(566, 184, cached=140, reasoning=70),
             summary="把风控/审批拒绝转成用户可读说明",
             content={"input": "governance: RISK_BLOCKED; approval: rejected …",
                      "output": "抱歉，该转账被风控拦截…本次操作未执行。如需继续，请走线下大额转账审核流程。"}),
    ]
    return finalize({
        "id": "risk-block", "title": "风控拦截 · 转账被拒",
        "subtitle": "高风险收款方 + 大额 → 预检拦截 → 人工审批拒绝",
        "agent": "db_agent", "harness": "Qoder Agent Harness",
        "turns": turns, "calls": calls,
        "meta": {"tools": ["transfer"], "models": ["llama-local", "deepseek-v4-flash"],
                 "highlights": ["风控预检拦截", "人工审批拒绝", "2 处错误态"]},
    })


# --------------------------------------------------------------------------- #
def build_all() -> list[dict]:
    calib = read_calibration()
    traces = [trace_orders_transfer(), trace_rag_qa(), trace_risk_block()]
    for t in traces:
        t["meta"]["calibration"] = calib
    return traces


def emit(traces: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(traces, ensure_ascii=False, indent=2)
    (DATA_DIR / "traces.json").write_text(payload, encoding="utf-8")
    js = ("// 由 build_trace.py 自动生成，请勿手改。\n"
          "// 以 <script> 加载，使 index.html 双击（file://）即可运行，无需服务器。\n"
          "window.TRACES = " + json.dumps(traces, ensure_ascii=False) + ";\n")
    (DATA_DIR / "traces.js").write_text(js, encoding="utf-8")


def main() -> None:
    # Windows 控制台默认 GBK，重配 stdout 为 UTF-8，避免中文/«» 打印报 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description="生成 Agent 对话轨迹示例数据")
    ap.add_argument("--stats", action="store_true", help="打印 observability.db 真实延迟统计")
    args = ap.parse_args()
    traces = build_all()
    emit(traces)
    print(f"[build_trace] 已生成 {len(traces)} 条轨迹 -> {DATA_DIR.relative_to(REPO_ROOT)}")
    for t in traces:
        print(f"  - {t['id']:<16} 轮次={t['turn_count']} 调用={t['call_count']} "
              f"错误={t['error_count']} 总时长={t['total_ms']/1000:.2f}s  «{t['title']}»")
    if args.stats:
        print("[build_trace] 真实延迟校准:", json.dumps(traces[0]["meta"]["calibration"], ensure_ascii=False))


if __name__ == "__main__":
    main()