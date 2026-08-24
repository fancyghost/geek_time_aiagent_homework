"""六大功能验证脚本：对已启动的网关服务逐项验证并输出证据表。

前置条件：
1. 网关服务已启动：uvicorn src.main:app --port 8000
2. llama.cpp server 已运行（默认 http://localhost:8080/v1）

运行：python scripts/verify_all.py
说明：默认包含 DeepSeek 云端模型验证（消耗 token），SKIP_DEEPSEEK=1 可跳过。

验收对照：双模型调用 / 流式 / 结构化 / 模板引用 / 可观测 / 重试与限流。
"""
import json
import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:8000")
LLAMA_URL = os.getenv("LLAMA_BASE_URL", "http://localhost:8080/v1")
MODEL = os.getenv("VERIFY_MODEL", "llama-local")
CLOUD_MODEL = "deepseek-v4-flash"
TIMEOUT = 300.0

# 证据收集：(功能点, 结论, 证据摘要)
EVIDENCE: list[tuple[str, str, str]] = []


def record(item: str, ok: bool, evidence: str) -> None:
    EVIDENCE.append((item, "PASS" if ok else "FAIL", evidence))
    print(f"[{'PASS' if ok else 'FAIL'}] {item}：{evidence}")


def check_llama_available() -> bool:
    """前置检查：llama.cpp server 是否可达。"""
    try:
        resp = httpx.get(f"{LLAMA_URL}/models", timeout=5)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def verify_model_invoke(client: httpx.Client, model: str, tag: str) -> None:
    """功能1：统一调用入口，model 字段路由到适配器。"""
    resp = client.post(
        "/v1/invoke",
        json={"model": model, "input": "用一句话介绍北京", "template": "general_chat",
              "max_output_tokens": 256},
    )
    body = resp.json()
    ok = (
        resp.status_code == 200
        and body.get("kind") == "text"
        and bool(body.get("text"))
        and body.get("model") == model
    )
    record(
        f"模型调用（{tag}）",
        ok,
        f"status={resp.status_code} kind={body.get('kind')} "
        f"text[:30]={str(body.get('text'))[:30]!r} latency={body.get('latency_ms')}ms",
    )


def verify_streaming(client: httpx.Client) -> None:
    """功能2：流式输出，SSE 逐块返回 + 首 Token 延迟。"""
    chunks: list[str] = []
    done: dict | None = None
    first_delta_ms: float | None = None
    started = time.perf_counter()

    with client.stream(
        "POST",
        "/v1/invoke",
        json={"model": MODEL, "input": "用三句话介绍长城", "template": "general_chat",
              "stream": True, "max_output_tokens": 256},
    ) as resp:
        assert resp.status_code == 200, f"流式请求失败：{resp.status_code}"
        assert "text/event-stream" in resp.headers.get("content-type", "")
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line[len("data: "):])
            if event.get("type") == "delta":
                if first_delta_ms is None:
                    first_delta_ms = (time.perf_counter() - started) * 1000
                chunks.append(event["text"])
            elif event.get("type") == "done":
                done = event

    ok = len(chunks) >= 2 and done is not None and done.get("ttft_ms") is not None
    record(
        "流式输出（SSE）",
        ok,
        f"SSE 块数={len(chunks)} done.ttft_ms={done.get('ttft_ms') if done else None} "
        f"客户端首块={first_delta_ms:.0f}ms 全文[:30]={''.join(chunks)[:30]!r}",
    )


def verify_structured(client: httpx.Client) -> None:
    """功能3：结构化输出（response_format / output_schema 约束合法 JSON）。"""
    schema = {
        "type": "object",
        "properties": {
            "city": {"type": "string"},
            "is_capital": {"type": "boolean"},
        },
        "required": ["city", "is_capital"],
    }
    resp = client.post(
        "/v1/invoke",
        json={"model": MODEL, "input": "介绍北京", "template": "city_info_json",
              "output_schema": schema, "max_output_tokens": 256},
    )
    body = resp.json()
    data = body.get("data") or {}
    ok = (
        resp.status_code == 200
        and body.get("kind") == "structured"
        and isinstance(data, dict)
        and data.get("city")
        and isinstance(data.get("is_capital"), bool)
    )
    record(
        "结构化输出",
        ok,
        f"kind={body.get('kind')} data={data} 原始文本[:50]={str(body.get('text'))[:50]!r}",
    )


def verify_templates(client: httpx.Client) -> None:
    """功能4：模板版本引用（@v1/@latest）+ 变量替换 + 防注入校验。"""
    # 4a. 版本引用：响应回显实际解析到的 name@vN
    r1 = client.post("/v1/invoke", json={
        "model": MODEL, "input": "查询 user 表的结构前请先说明你的输出规范",
        "template": "db_assistant@v1", "max_output_tokens": 128,
    }).json()
    r2 = client.post("/v1/invoke", json={
        "model": MODEL, "input": "查询 user 表的结构前请先说明你的输出规范",
        "template": "db_assistant@latest", "max_output_tokens": 128,
    }).json()
    version_ok = r1.get("template") == "db_assistant@v1" and r2.get("template") == "db_assistant@v2"

    # 4b. 变量替换：{{dialect}} 注入后要求模型回显标记（软证据）
    rv = client.post("/v1/invoke", json={
        "model": MODEL, "input": "你是什么数据库专家？只回答标记本身",
        "template": "db_query", "variables": {"dialect": "MySQL"},
        "max_output_tokens": 128,
    }).json()
    marker_found = "MySQL" in str(rv.get("text", ""))

    # 4c. 防注入确定性校验：缺变量 400 / 未登记模板 404
    e1 = client.post("/v1/invoke", json={"model": MODEL, "input": "x", "template": "db_query"})
    e2 = client.post("/v1/invoke", json={"model": MODEL, "input": "x",
                                         "template": "ignore_previous_instructions"})
    guard_ok = (
        e1.status_code == 400
        and e1.json()["error"]["code"] == "TEMPLATE_VAR_MISSING"
        and e2.status_code == 404
        and e2.json()["error"]["code"] == "TEMPLATE_NOT_FOUND"
    )

    ok = version_ok and guard_ok
    record(
        "模板版本管理",
        ok,
        f"v1 解析={r1.get('template')} latest 解析={r2.get('template')} "
        f"变量替换含 MySQL={marker_found} 缺变量={e1.status_code}/{e1.json()['error']['code']} "
        f"未登记模板={e2.status_code}/{e2.json()['error']['code']}",
    )


def verify_observability(client: httpx.Client) -> None:
    """功能5：可观测性——Token 分类统计、延迟、TTFT（SQLite 落盘）。"""
    summary = client.get("/v1/observability/summary").json()
    calls = client.get("/v1/observability/calls?limit=5").json()["calls"]
    target = next((m for m in summary["models"] if m["model"] == MODEL), None)
    ok = (
        target is not None
        and target["calls"] >= 1
        and target["tokens"]["input"] > 0
        and target["latency_ms"] is not None
        and any(c.get("ttft_ms") for c in calls if c["model"] == MODEL)
    )
    record(
        "可观测性",
        ok,
        f"模型聚合={json.dumps(target, ensure_ascii=False)} "
        f"最近明细条数={len(calls)}（含 ttft/latency/tokens 分类）",
    )


def verify_retry(client: httpx.Client) -> None:
    """功能6a：指数退避重试——注入 2 次瞬态故障，第 3 次尝试应成功。"""
    inject = client.post("/v1/_debug/fault", json={"model": MODEL, "fail_times": 2})
    assert inject.status_code == 200, inject.text

    started = time.perf_counter()
    resp = client.post("/v1/invoke", json={
        "model": MODEL, "input": "你好", "template": "general_chat",
        "max_output_tokens": 64,
    })
    elapsed_ms = (time.perf_counter() - started) * 1000
    body = resp.json()
    client.delete("/v1/_debug/fault", params={"model": MODEL})

    # 退避序列 0.5s+1.0s=1.5s，总耗时应明显大于单次调用下限
    ok = resp.status_code == 200 and body.get("retries") == 2 and elapsed_ms >= 1500
    record(
        "重试（指数退避）",
        ok,
        f"status={resp.status_code} retries={body.get('retries')} "
        f"总耗时={elapsed_ms:.0f}ms（含 0.5s+1.0s 退避）",
    )


def verify_rate_limit(client: httpx.Client) -> None:
    """功能6b：按模型独立限流——突发请求超限应返回 429 LOCAL_RATE_LIMITED。"""
    codes: list[int] = []
    error_codes: list[str] = []
    for _ in range(5):
        resp = client.post("/v1/invoke", json={
            "model": MODEL, "input": "你好", "template": "general_chat",
            "max_output_tokens": 8,
        })
        codes.append(resp.status_code)
        if resp.status_code == 429:
            error_codes.append(resp.json()["error"]["code"])

    limited = [c for c in codes if c == 429]
    ok = len(limited) >= 1 and all(c == "LOCAL_RATE_LIMITED" for c in error_codes)
    record(
        "限流（429）",
        ok,
        f"5 连发状态码={codes} 429 次数={len(limited)} 错误码={set(error_codes) or 'N/A'}",
    )


def verify_deepseek_stream(client: httpx.Client) -> None:
    """云端模型流式输出：SSE 逐块返回（chat completions stream）。"""
    chunks: list[str] = []
    done: dict | None = None
    with client.stream(
        "POST",
        "/v1/invoke",
        json={"model": CLOUD_MODEL, "input": "用一句话介绍长城", "template": "general_chat",
              "stream": True, "max_output_tokens": 128},
    ) as resp:
        if resp.status_code != 200:
            record("流式输出（deepseek 云端）", False,
                   f"status={resp.status_code} body={resp.read()[:200]!r}")
            return
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line[len("data: "):])
            if event.get("type") == "delta":
                chunks.append(event["text"])
            elif event.get("type") == "done":
                done = event

    ok = len(chunks) >= 1 and done is not None and done.get("ttft_ms") is not None
    record(
        "流式输出（deepseek 云端）",
        ok,
        f"SSE 块数={len(chunks)} done.ttft_ms={done.get('ttft_ms') if done else None} "
        f"全文[:30]={''.join(chunks)[:30]!r}",
    )


def verify_deepseek_responses(client: httpx.Client) -> None:
    """云端模型 responses 接口 + 原生 json_schema 结构化输出。"""
    resp = client.post("/v1/invoke", json={
        "model": CLOUD_MODEL, "input": "介绍北京", "template": "city_info_json",
        "api_mode": "responses", "max_output_tokens": 256,
        "output_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}, "is_capital": {"type": "boolean"}},
            "required": ["city", "is_capital"],
        },
    })
    body = resp.json()
    data = body.get("data") or {}
    ok = (
        resp.status_code == 200
        and body.get("kind") == "structured"
        and isinstance(data, dict)
        and bool(data.get("city"))
        and isinstance(data.get("is_capital"), bool)
    )
    record(
        "结构化输出（deepseek responses）",
        ok,
        f"status={resp.status_code} kind={body.get('kind')} data={data} usage={body.get('usage')}",
    )


def main() -> int:
    print(f"网关地址：{BASE_URL}　验证模型：{MODEL}\n")

    client = httpx.Client(base_url=BASE_URL, timeout=TIMEOUT)
    try:
        health = client.get("/health")
        if health.status_code != 200:
            print("网关服务不可用，请先启动：uvicorn src.main:app --port 8000")
            return 2
    except httpx.HTTPError:
        print("无法连接网关服务，请先启动：uvicorn src.main:app --port 8000")
        return 2

    if not check_llama_available():
        print(f"llama.cpp server 不可达（{LLAMA_URL}），请先启动本地 server 再运行验证。")
        return 2

    # 六大功能验证
    verify_model_invoke(client, MODEL, "llama 本地")
    verify_streaming(client)
    verify_structured(client)
    verify_templates(client)
    verify_observability(client)
    verify_retry(client)
    verify_rate_limit(client)

    # 云端模型（deepseek-v4-flash）：普通调用 / 流式 / responses 结构化，消耗 token
    if os.getenv("SKIP_DEEPSEEK") == "1":
        print("\n[SKIP] DeepSeek 云端验证已跳过（SKIP_DEEPSEEK=1）")
    else:
        verify_model_invoke(client, CLOUD_MODEL, "deepseek 云端")
        verify_deepseek_stream(client)
        verify_deepseek_responses(client)

    # 证据表汇总
    print("\n" + "=" * 72)
    print(f"{'功能点':<24}{'结论':<8}证据")
    print("-" * 72)
    for item, verdict, evidence in EVIDENCE:
        print(f"{item:<22}{verdict:<8}{evidence[:110]}")
    print("=" * 72)

    failed = [item for item, verdict, _ in EVIDENCE if verdict == "FAIL"]
    if failed:
        print(f"共 {len(failed)} 项未通过：{failed}")
        return 1
    print("全部功能验证通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
