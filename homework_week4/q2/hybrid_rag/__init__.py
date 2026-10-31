"""q2 · 基于 LlamaIndex 的向量 + BM25 混合检索 RAG（第二题核心包）。

对外导出常用入口，便于 run_demo.py 与 tests 直接调用：
  * config     —— 路径 / 模型 / 检索超参 / 有效期常量
  * materials  —— 只读资料 → 工作副本、有效期改写
  * indexing   —— 读取分块、向量索引、保存/加载
  * retrieval  —— 向量 / BM25 / RRF 混合三路检索
  * answering  —— 把混合检索证据交给本地 LLM 生成带来源回答
  * embedder   —— 本地 HuggingFace 真实 Embedding
  * llm        —— 本地 llama.cpp（OpenAI 兼容）LLM
"""
from __future__ import annotations

from . import answering, config, embedder, indexing, llm, materials, retrieval, tokenizer

__all__ = [
    "answering",
    "config",
    "embedder",
    "indexing",
    "llm",
    "materials",
    "retrieval",
    "tokenizer",
]
