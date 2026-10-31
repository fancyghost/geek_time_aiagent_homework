"""q2 混合检索 RAG 的 pytest 验证。

对齐作业三个关键点，做到「离线、确定性、快」：
  1. 分块 + 来源  ：build_nodes 产出带 source/chunk_index 元数据的节点；
  2. 双索引 + RRF ：同一批节点分别建向量索引与 BM25，混合检索能融合两路并命中关键块；
  3. 更新退出旧块 ：把有效期 30→20 分钟后，重建的节点里旧短语彻底消失、新短语出现。

为避免测试依赖 ~1.3GB 真实模型与网络，向量部分统一用 MockEmbedding（确定性、离线）；
真实 Embedding 的维度自检放在 test_real_embedder_dim，未下载模型时自动跳过。

运行（在 q2/ 目录下）：
    python -m pytest -q
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# 让测试能导入同级的 hybrid_rag 包（q2/ 加入 sys.path）
BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import pytest  # noqa: E402

from hybrid_rag import config, indexing, materials, retrieval  # noqa: E402
from hybrid_rag.tokenizer import tokenize  # noqa: E402


# --------------------------------------------------------------------------- #
# 工具：把只读资料拷到临时目录，测试全程不碰仓库里的 login_docs / .rag_workspace
# --------------------------------------------------------------------------- #
def _prep_docs(tmp_path: Path) -> Path:
    dst = tmp_path / "login_docs"
    shutil.copytree(config.PRISTINE_DOCS, dst)
    return dst


def _mock_embedding(dim: int = 16):
    """确定性、离线的假 Embedding（避免测试触发真实模型下载）。"""
    from llama_index.core.embeddings import MockEmbedding

    return MockEmbedding(embed_dim=dim)


# --------------------------------------------------------------------------- #
# 1. 中文分词：BM25 的基础
# --------------------------------------------------------------------------- #
def test_chinese_tokenizer_hits_keywords():
    toks = tokenize("登录会话的有效期为 30 分钟，采用滑动过期。")
    # jieba 应把领域词切成独立词元，且停用词「的/为」被过滤
    assert "有效期" in toks
    assert "会话" in toks
    assert "登录" in toks
    assert "30" in toks
    assert "的" not in toks


# --------------------------------------------------------------------------- #
# 2. 分块 + 来源元数据
# --------------------------------------------------------------------------- #
def test_build_nodes_carry_source_and_chunk_index(tmp_path):
    docs = indexing.load_documents(_prep_docs(tmp_path))
    assert len(docs) == 3  # 登录与会话策略 / 账号安全与风控 / 常见问题FAQ
    nodes = indexing.build_nodes(docs)

    assert len(nodes) > 0
    for n in nodes:
        # 每个节点都带来源与块序号（"带来源分块"的基础）
        assert n.metadata.get("source")
        assert isinstance(n.metadata.get("chunk_index"), int)
        assert n.get_content().strip()

    # 三个来源都应至少贡献一个分块
    sources = {n.metadata["source"] for n in nodes}
    assert "登录与会话策略.md" in sources
    assert "账号安全与风控.md" in sources
    assert "常见问题FAQ.md" in sources

    # 含答案的关键块确实存在（有效期 30 分钟）
    assert any("有效期" in n.get_content() and "30" in n.get_content() for n in nodes)


# --------------------------------------------------------------------------- #
# 3. BM25（jieba）检索命中关键块
# --------------------------------------------------------------------------- #
def test_bm25_retriever_hits_validity_chunk(tmp_path):
    docs = indexing.load_documents(_prep_docs(tmp_path))
    nodes = indexing.build_nodes(docs)
    bm25 = retrieval.build_bm25_retriever(nodes, top_k=5)

    hits = retrieval.retrieve(bm25, config.QUESTION)
    assert len(hits) > 0
    joined = "\n".join(h.node.get_content() for h in hits)
    # 词面匹配应把「有效期 / 30 分钟」相关块排进来
    assert "有效期" in joined
    assert any(h.node.metadata.get("source") == "登录与会话策略.md" for h in hits)


# --------------------------------------------------------------------------- #
# 4. 双索引 + RRF 融合（向量用 MockEmbedding，离线确定性）
# --------------------------------------------------------------------------- #
def test_dual_index_and_rrf_fusion(tmp_path):
    docs = indexing.load_documents(_prep_docs(tmp_path))
    nodes = indexing.build_nodes(docs)

    index = indexing.build_vector_index(nodes, embed_model=_mock_embedding())
    vec = retrieval.build_vector_retriever(index, top_k=5)
    bm25 = retrieval.build_bm25_retriever(nodes, top_k=5)
    hybrid = retrieval.build_hybrid_retriever(vec, bm25, top_k=5)

    vec_hits = retrieval.retrieve(vec, config.QUESTION)
    bm25_hits = retrieval.retrieve(bm25, config.QUESTION)
    hyb_hits = retrieval.retrieve(hybrid, config.QUESTION)

    assert len(vec_hits) > 0
    assert len(bm25_hits) > 0
    assert len(hyb_hits) > 0

    # RRF 融合了两路：结果里应同时出现「向量能召回」与「BM25 命中有效期」的证据
    hyb_text = "\n".join(h.node.get_content() for h in hyb_hits)
    assert "有效期" in hyb_text

    # format_hits 产出结构化、带来源、带排名的字典（供演示打印/落盘）
    formatted = retrieval.format_hits(hyb_hits)
    assert formatted[0]["rank"] == 1
    assert all("source" in h and "text" in h for h in formatted)


# --------------------------------------------------------------------------- #
# 5. 索引持久化：保存后能加载回同一批节点
# --------------------------------------------------------------------------- #
def test_vector_index_persist_roundtrip(tmp_path):
    docs = indexing.load_documents(_prep_docs(tmp_path))
    nodes = indexing.build_nodes(docs)
    index = indexing.build_vector_index(nodes, embed_model=_mock_embedding())

    persist_dir = tmp_path / "index_store"
    saved = indexing.save_index(index, persist_dir=persist_dir)
    assert saved.exists()
    assert any(saved.glob("*"))  # 目录里确有持久化文件

    loaded = indexing.load_index(persist_dir=persist_dir, embed_model=_mock_embedding())
    loaded_nodes = indexing.nodes_from_index(loaded)
    assert len(loaded_nodes) == len(nodes)


# --------------------------------------------------------------------------- #
# 6. 更新验证：30→20 分钟后，旧短语退出、新短语进入
# --------------------------------------------------------------------------- #
def test_validity_update_retires_old_phrase(tmp_path):
    docs_dir = _prep_docs(tmp_path)

    # 更新前：原始资料里含 "30 分钟"，不含 "20 分钟"
    before = materials.read_docs(docs_dir)
    assert config.original_validity_phrase() in "\n".join(before.values())

    report = materials.apply_validity_update(config.UPDATED_VALIDITY_MINUTES, docs_dir=docs_dir)
    assert report.replacements >= 1
    assert report.changed_files  # 至少改了「登录与会话策略.md」「常见问题FAQ.md」

    # 更新后：旧短语彻底消失，新短语出现
    after = materials.read_docs(docs_dir)
    joined = "\n".join(after.values())
    assert config.original_validity_phrase() not in joined
    assert config.updated_validity_phrase() in joined

    # 重建节点后，旧短语在"当前检索语料"里为 0（旧分块已退出）
    docs = indexing.load_documents(docs_dir)
    nodes = indexing.build_nodes(docs)
    stale = [n for n in nodes if config.original_validity_phrase() in n.get_content()]
    fresh = [n for n in nodes if config.updated_validity_phrase() in n.get_content()]
    assert len(stale) == 0
    assert len(fresh) >= 1

    # 干扰项不受影响：账号安全里的「锁定 15 分钟」仍在
    assert "15 分钟" in after.get("账号安全与风控.md", "")


# --------------------------------------------------------------------------- #
# 7. 只读资料不被测试污染（reset_workspace 只动 .rag_workspace）
# --------------------------------------------------------------------------- #
def test_pristine_docs_untouched_by_update(tmp_path):
    docs_dir = _prep_docs(tmp_path)
    materials.apply_validity_update(config.UPDATED_VALIDITY_MINUTES, docs_dir=docs_dir)
    # 仓库里的只读资料仍是 30 分钟（改写只发生在副本上）
    pristine = "\n".join(materials.read_docs(config.PRISTINE_DOCS).values())
    assert config.original_validity_phrase() in pristine


# --------------------------------------------------------------------------- #
# 8. 真实 Embedding 维度自检
#    构造 HuggingFaceEmbedding 会触发 ~1.3GB 模型下载，绝不能让常规测试卡在这里；
#    故默认跳过，仅当显式设置 RAG_TEST_REAL_EMBED=1（模型已下载）时才真正自检。
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    os.environ.get("RAG_TEST_REAL_EMBED") != "1",
    reason="真实 Embedding 自检需下载 ~1.3GB 模型；设 RAG_TEST_REAL_EMBED=1 后再跑（默认跳过）",
)
def test_real_embedder_dim():
    from hybrid_rag.embedder import embed_dim

    assert embed_dim() == 1024  # bge-large-zh-v1.5
