"""索引构建：读取资料 → 分块 → 向量索引 → 保存/加载。

- 每个 .md 文件是一个 Document，metadata.source 记录来源文件名（用于"带来源"的引用）。
- 用 SentenceSplitter 按 chunk_size/overlap 分块；把 source 等元数据排除在"被向量化文本"之外，
  避免污染语义向量，同时保留它用于展示来源。
- 向量索引用真实 Embedding 构建，并可 persist 到磁盘；加载后可从 docstore 取回节点，
  据此重建 BM25（BM25 索引是内存态、由节点即时构建）。
"""
from __future__ import annotations

import re
from pathlib import Path

from . import config

# 不参与向量化/LLM 文本拼接的元数据键（仅用于展示与来源标注）
_NON_TEXT_META = ("source", "file_path", "chunk_index")

# 中文句子切分：在句末标点或换行之后断开。用于替换 SentenceSplitter 默认的
# nltk punkt 分句器——nltk_data 常托管在 github（本机不可达），故完全绕开 nltk。
_SENT_BOUNDARY = re.compile(r"(?<=[。！？；?!;\n])")


def chinese_sentence_splitter(text: str) -> list[str]:
    """把文本按中文句末标点/换行切成句子单元（保留分隔符，去空）。"""
    parts = _SENT_BOUNDARY.split(text)
    return [p for p in parts if p and p.strip()]


def load_documents(docs_dir: Path | None = None):
    """读取资料目录下全部 .md，构造带来源元数据的 Document 列表（按文件名排序）。"""
    from llama_index.core import Document

    d = Path(docs_dir or config.WORK_DOCS)
    docs = []
    for path in sorted(d.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        docs.append(
            Document(
                text=text,
                id_=f"doc::{path.name}",
                metadata={"source": path.name, "file_path": str(path)},
            )
        )
    return docs


def build_nodes(documents, chunk_size: int | None = None, chunk_overlap: int | None = None):
    """把 Document 分块成 TextNode，并为每个节点补齐来源与块序号元数据。"""
    from llama_index.core.node_parser import SentenceSplitter

    splitter = SentenceSplitter(
        chunk_size=chunk_size or config.CHUNK_SIZE,
        chunk_overlap=chunk_overlap or config.CHUNK_OVERLAP,
        chunking_tokenizer_fn=chinese_sentence_splitter,
    )
    nodes = splitter.get_nodes_from_documents(documents, show_progress=False)
    for i, n in enumerate(nodes):
        n.metadata.setdefault("source", n.metadata.get("file_name", "未知来源"))
        n.metadata["chunk_index"] = i
        # 让来源类元数据不进入被向量化/被 LLM 拼接的正文
        n.excluded_embed_metadata_keys = list(_NON_TEXT_META)
        n.excluded_llm_metadata_keys = list(_NON_TEXT_META)
    return nodes


def build_vector_index(nodes, embed_model=None):
    """基于真实 Embedding 构建向量索引。"""
    from llama_index.core import VectorStoreIndex

    if embed_model is None:
        from .embedder import get_embed_model

        embed_model = get_embed_model()
    return VectorStoreIndex(nodes, embed_model=embed_model)


def save_index(index, persist_dir: Path | None = None) -> Path:
    """把向量索引与 docstore 持久化到磁盘。"""
    d = Path(persist_dir or config.INDEX_DIR)
    if d.exists():
        import shutil

        shutil.rmtree(d)
    d.mkdir(parents=True, exist_ok=True)
    index.storage_context.persist(persist_dir=str(d))
    return d


def load_index(persist_dir: Path | None = None, embed_model=None):
    """从磁盘加载向量索引（需提供同一 Embedding 模型）。"""
    from llama_index.core import StorageContext, load_index_from_storage

    d = Path(persist_dir or config.INDEX_DIR)
    sc = StorageContext.from_defaults(persist_dir=str(d))
    if embed_model is None:
        from .embedder import get_embed_model

        embed_model = get_embed_model()
    return load_index_from_storage(sc, embed_model=embed_model)


def nodes_from_index(index) -> list:
    """从（已加载的）索引 docstore 取回全部节点，用于重建 BM25。"""
    return list(index.docstore.docs.values())
