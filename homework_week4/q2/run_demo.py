"""q2 · 第二题演示：基于 LlamaIndex 的「向量 + BM25」混合检索 RAG（三步验证）。

  步骤 1（建库结果）      : 读取当前项目的登录资料并分块，基于同一批节点建立向量索引与
                            BM25 关键词索引；用真实 Embedding，保存索引，打印节点数量与
                            一条带来源的分块样例。
  步骤 2（三路查询对比与回答）: 分别用向量 / BM25 / RRF 混合三种检索查询
                            "登录会话的有效期是多少分钟？"，各打印前 5 条结果及来源；
                            再把混合检索的证据交给本地真实模型，生成带来源引用的回答。
  步骤 3（更新后的回答）  : 在作业资料副本中把登录会话有效期改为 20 分钟，同步重建两路索引，
                            确保旧分块退出当前检索；重新查询同一问题，输出更新后的回答与引用。

设计（与第一题 demo_project → .demo_workspace 同构，保证可重复运行）：
  * login_docs/ 是仓库里提交的**只读初始资料**（有效期 30 分钟），演示绝不改它；
  * 每次运行先整目录拷贝到 .rag_workspace/login_docs/，第 3 步在副本上改写为 20 分钟；
  * Embedding 走本地 HuggingFace 真实模型；生成回答的 LLM 走本地 llama.cpp（需先启动服务）。

运行（在 q2/ 目录下，PowerShell 用 ; 分隔；建议先 chcp 65001）：
    python run_demo.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from hybrid_rag import answering, config, indexing, llm as llm_mod, materials, retrieval  # noqa: E402
from hybrid_rag.embedder import get_embed_model  # noqa: E402


# --------------------------------------------------------------------------- #
# 控制台 UTF-8 / Tee 干净转录 / 打印工具（沿用第一题已验证的做法）
# --------------------------------------------------------------------------- #
def _force_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type:ignore[attr-defined]
        except Exception:
            pass


def _enable_utf8_console() -> None:
    if os.name == "nt":
        try:
            # 直接调用 chcp.com（不经 shell），把控制台代码页切到 UTF-8(65001)；
            # 子进程与父进程共享同一控制台，故对当前 Python 进程同样生效。
            subprocess.run(["chcp.com", "65001"], timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


class _Tee:
    """把 stdout 同时写到控制台与一个 UTF-8 文件，产出不受终端编码影响的干净转录。"""

    def __init__(self, orig, fh) -> None:
        self.orig = orig
        self.fh = fh

    def write(self, s: str) -> int:
        self.orig.write(s)
        self.fh.write(s)
        return len(s)

    def flush(self) -> None:
        self.orig.flush()
        self.fh.flush()


def _print_header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def _save_artifact(name: str, payload: dict[str, Any]) -> Path:
    config.ARTIFACTS.mkdir(parents=True, exist_ok=True)
    path = config.ARTIFACTS / name
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


def _clip(text: str, n: int = 78) -> str:
    t = " ".join(text.split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _print_hits(title: str, hits: list[dict]) -> None:
    print(f"\n---- {title} ----")
    for h in hits:
        score = h.get("score")
        score_s = f"{score:.4f}" if isinstance(score, (int, float)) else "-"
        print(f"  #{h['rank']}  score={score_s}  来源={h['source']}")
        print(f"        {_clip(h['text'])}")


# --------------------------------------------------------------------------- #
# 步骤 1：建库结果
# --------------------------------------------------------------------------- #
def step1_build() -> dict[str, Any]:
    _print_header("步骤 1 · 建库结果：读取登录资料并分块，建立向量 + BM25 双索引")

    work_docs = materials.reset_workspace()
    print(f"[RAG] 只读初始资料 : {config.PRISTINE_DOCS}")
    print(f"[RAG] 工作副本资料 : {work_docs}")

    docs = indexing.load_documents(work_docs)
    print(f"[RAG] 读取资料文件 {len(docs)} 个：")
    for d in docs:
        print(f"        - {d.metadata['source']}（{len(d.text)} 字符）")

    embed_model = get_embed_model()
    dim = len(embed_model.get_text_embedding("维度自检"))
    print(f"[RAG] Embedding 模型: {config.EMBED_MODEL_NAME}（本地 HuggingFace 真实模型，向量维度={dim}）")
    print(f"[RAG] 分块参数      : chunk_size={config.CHUNK_SIZE} overlap={config.CHUNK_OVERLAP}")

    nodes = indexing.build_nodes(docs)
    per_source: dict[str, int] = {}
    for n in nodes:
        src = n.metadata.get("source", "未知来源")
        per_source[src] = per_source.get(src, 0) + 1
    print(f"[RAG] 节点总数      : {len(nodes)}")
    for src, c in sorted(per_source.items()):
        print(f"        - {src}: {c} 个分块")

    # 向量索引 + BM25 索引（同一批节点）
    t0 = time.time()
    index = indexing.build_vector_index(nodes, embed_model=embed_model)
    print(f"[RAG] 向量索引已建立（{len(nodes)} 节点，用时 {time.time() - t0:.1f}s）")
    bm25 = retrieval.build_bm25_retriever(nodes)
    print(f"[RAG] BM25 关键词索引已建立（jieba 中文分词，{len(nodes)} 节点）")

    # 保存索引到磁盘
    save_dir = indexing.save_index(index)
    saved_files = sorted(p.name for p in save_dir.glob("*"))
    print("\n---- 索引已保存到磁盘 ----")
    print(f"index_dir = {save_dir}")
    print(f"files     = {saved_files}")

    # 一条带来源的分块样例（优先挑含"有效期"的关键块）
    sample = next((n for n in nodes if "有效期" in n.get_content()), nodes[0])
    print("\n---- 一条带来源的分块样例 ----")
    print(f"来源(source)  = {sample.metadata.get('source')}")
    print(f"块序号(chunk) = {sample.metadata.get('chunk_index')}")
    print(f"节点 id       = {sample.node_id}")
    print(f"正文          =\n{_indent(sample.get_content().strip())}")

    artifact = _save_artifact(
        "step1_build.json",
        {
            "embed_model": config.EMBED_MODEL_NAME,
            "embed_dim": dim,
            "chunk_size": config.CHUNK_SIZE,
            "chunk_overlap": config.CHUNK_OVERLAP,
            "doc_count": len(docs),
            "docs": [d.metadata["source"] for d in docs],
            "node_count": len(nodes),
            "nodes_per_source": per_source,
            "index_dir": str(save_dir),
            "index_files": saved_files,
            "sample_chunk": {
                "source": sample.metadata.get("source"),
                "chunk_index": sample.metadata.get("chunk_index"),
                "node_id": sample.node_id,
                "text": sample.get_content().strip(),
            },
        },
    )
    print(f"\n[产物] {artifact}")

    vec = retrieval.build_vector_retriever(index)
    hybrid = retrieval.build_hybrid_retriever(vec, bm25)
    return {"index": index, "nodes": nodes, "vector": vec, "bm25": bm25, "hybrid": hybrid}


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + ln for ln in text.splitlines())


# --------------------------------------------------------------------------- #
# 步骤 2：三路查询对比与回答
# --------------------------------------------------------------------------- #
def step2_query(ctx: dict[str, Any]) -> dict[str, Any]:
    _print_header("步骤 2 · 三路查询对比与回答：向量 / BM25 / RRF 混合")

    question = config.QUESTION
    print(f"[问题] {question}")

    info = llm_mod.server_info()
    if info.get("ok"):
        print(f"[LLM] 本地 llama.cpp 就绪：{info['base_url']}  models={info.get('models')}")
    else:
        print(f"[LLM] 本地 llama.cpp 未就绪：{info['base_url']}（{info.get('error')}）")
        print("      → 检索对比照常展示；生成回答这一步需要先启动 llama-server 后重跑。")

    vec_hits = retrieval.format_hits(retrieval.retrieve(ctx["vector"], question))
    bm25_hits = retrieval.format_hits(retrieval.retrieve(ctx["bm25"], question))
    hyb_hits = retrieval.format_hits(retrieval.retrieve(ctx["hybrid"], question))

    _print_hits(f"① 向量检索 Top{config.TOP_K}（语义相似）", vec_hits)
    _print_hits(f"② BM25 关键词检索 Top{config.TOP_K}（词面匹配）", bm25_hits)
    _print_hits(f"③ RRF 混合检索 Top{config.TOP_K}（融合①②）", hyb_hits)

    # 用混合检索证据交给本地真实模型，生成带来源回答
    print("\n---- 用混合检索(RRF)证据生成带来源回答 ----")
    hyb_nodes = retrieval.retrieve(ctx["hybrid"], question)
    if info.get("ok"):
        ans = answering.generate_answer(llm_mod.get_llm(), question, hyb_nodes)
        print(ans.answer)
        print(f"\n[引用] 回答引用了证据序号 {ans.citations}（来源："
              f"{[ans.evidence[i - 1].source for i in ans.citations]}）")
        answer_dict = ans.to_dict()
    else:
        answer_dict = {
            "question": question,
            "answer": "（本地 llama.cpp 未启动，无法生成回答；请启动 llama-server 后重跑本步）",
            "citations": [],
            "evidence": [e.to_dict() for e in answering.build_evidence_blocks(hyb_nodes)],
            "llm_unavailable": True,
        }
        print(answer_dict["answer"])

    artifact = _save_artifact(
        "step2_query.json",
        {
            "question": question,
            "vector_top": vec_hits,
            "bm25_top": bm25_hits,
            "hybrid_rrf_top": hyb_hits,
            "answer": answer_dict,
            "llm": info,
        },
    )
    print(f"\n[产物] {artifact}")
    return {"vector": vec_hits, "bm25": bm25_hits, "hybrid": hyb_hits, "answer": answer_dict}


# --------------------------------------------------------------------------- #
# 步骤 3：更新验证
# --------------------------------------------------------------------------- #
def step3_update(ctx: dict[str, Any], prev_answer: dict[str, Any]) -> dict[str, Any]:
    _print_header("步骤 3 · 更新后的回答：有效期 30→20 分钟，重建双索引，重新查询")

    # 3.1 在作业资料副本中改写有效期
    report = materials.apply_validity_update(config.UPDATED_VALIDITY_MINUTES)
    print(f"[更新] 把「{report.from_phrase}」改为「{report.to_phrase}」")
    print(f"[更新] 命中文件 {len(report.changed_files)} 个，替换 {report.replacements} 处：{report.changed_files}")

    # 3.2 同步重建两路索引（向量 + BM25），旧的 30 分钟分块随之退出
    embed_model = get_embed_model()
    docs = indexing.load_documents(config.WORK_DOCS)
    nodes = indexing.build_nodes(docs)
    index = indexing.build_vector_index(nodes, embed_model=embed_model)
    bm25 = retrieval.build_bm25_retriever(nodes)
    vec = retrieval.build_vector_retriever(index)
    hybrid = retrieval.build_hybrid_retriever(vec, bm25)
    save_dir = indexing.save_index(index)
    print(f"[更新] 已重建向量 + BM25 双索引并保存：{save_dir}（节点数={len(nodes)}）")

    # 3.3 确认旧分块退出当前检索：全库不再包含旧有效期短语
    old_phrase = config.original_validity_phrase()
    stale = [n for n in nodes if old_phrase in n.get_content()]
    print(f"[核对] 重建后仍包含旧短语「{old_phrase}」的节点数 = {len(stale)}（应为 0，证明旧分块已退出）")

    # 3.4 重新查询同一问题
    question = config.QUESTION
    hyb_nodes = retrieval.retrieve(hybrid, question)
    hyb_hits = retrieval.format_hits(hyb_nodes)
    _print_hits(f"更新后 · RRF 混合检索 Top{config.TOP_K}", hyb_hits)

    print("\n---- 更新后的回答（本地真实模型 + 新资料引用）----")
    info = llm_mod.server_info()
    if info.get("ok"):
        ans = answering.generate_answer(llm_mod.get_llm(), question, hyb_nodes)
        print(ans.answer)
        print(f"\n[引用] 回答引用了证据序号 {ans.citations}（来源："
              f"{[ans.evidence[i - 1].source for i in ans.citations]}）")
        answer_dict = ans.to_dict()
    else:
        answer_dict = {
            "question": question,
            "answer": "（本地 llama.cpp 未启动，无法生成回答；请启动 llama-server 后重跑本步）",
            "citations": [],
            "evidence": [e.to_dict() for e in answering.build_evidence_blocks(hyb_nodes)],
            "llm_unavailable": True,
        }
        print(answer_dict["answer"])

    # 3.5 更新前 vs 更新后
    print("\n---- 更新前 vs 更新后 ----")
    print(f"  资料有效期 : {config.ORIGINAL_VALIDITY_MINUTES} 分钟 -> {config.UPDATED_VALIDITY_MINUTES} 分钟")
    print(f"  更新前回答 : {_clip(prev_answer.get('answer', ''), 90)}")
    print(f"  更新后回答 : {_clip(answer_dict.get('answer', ''), 90)}")
    print(f"  旧分块残留 : {len(stale)}（0 表示旧资料已退出当前检索）")

    artifact = _save_artifact(
        "step3_update.json",
        {
            "update_report": report.to_dict(),
            "stale_nodes_with_old_phrase": len(stale),
            "hybrid_rrf_top_after": hyb_hits,
            "answer_before": prev_answer,
            "answer_after": answer_dict,
        },
    )
    print(f"\n[产物] {artifact}")
    return {"hybrid": hyb_hits, "answer": answer_dict, "stale": len(stale), "report": report.to_dict()}


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main(argv: list[str]) -> int:
    _force_utf8_stdout()
    _enable_utf8_console()

    config.ARTIFACTS.mkdir(parents=True, exist_ok=True)
    transcript = config.ARTIFACTS / "console_transcript.txt"
    tee = _Tee(sys.stdout, transcript.open("w", encoding="utf-8"))
    sys.stdout = tee
    try:
        print("可用检索方式：向量(VectorStoreIndex) / BM25(bm25s+jieba) / RRF 混合(QueryFusionRetriever)")
        print(f"[配置] Embedding = {config.EMBED_MODEL_NAME}；LLM = 本地 llama.cpp @ {config.LLAMA_BASE_URL}")

        ctx = step1_build()
        s2 = step2_query(ctx)
        step3_update(ctx, s2["answer"])

        _print_header("演示完成 · 三张截图对应位置")
        print("  截图 1（建库结果）        : 步骤 1 的节点数量 + 带来源分块样例 + 索引保存路径；或 artifacts/step1_build.json")
        print("  截图 2（三路查询对比与回答）: 步骤 2 的向量/BM25/RRF 三路 Top5 + 混合检索带来源回答；或 artifacts/step2_query.json")
        print("  截图 3（更新后的回答）    : 步骤 3 的有效期 30→20、旧分块退出、更新后回答与引用；或 artifacts/step3_update.json")
        print(f"\n记忆/索引目录 : {config.INDEX_DIR}")
        print(f"干净 UTF-8 转录: {transcript}")
    finally:
        sys.stdout = tee.orig
        tee.fh.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
