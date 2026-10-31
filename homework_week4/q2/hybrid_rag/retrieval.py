"""三路检索：向量 / BM25 关键词 / RRF 混合。

- 向量检索：VectorStoreIndex.as_retriever（真实 Embedding，语义相似）。
- BM25 检索：JiebaBM25Retriever —— 标准 Okapi BM25 + jieba 中文分词。
  LlamaIndex 官方 BM25Retriever 底层是 bm25s，其 tokenizer 面向英文（按 \\b\\w+\\b 切词），
  对中文会把整句当成一个词、几乎无法命中；这里用 jieba 分词 + 经典 Okapi BM25 公式，
  返回真实节点（NodeWithScore），可无缝接入 QueryFusionRetriever 做 RRF 融合。
- RRF 混合：QueryFusionRetriever(mode="reciprocal_rerank", num_queries=1)，
  num_queries=1 表示只用原始问题、不做 LLM 查询改写，纯做两路结果的倒数排名融合。
"""
from __future__ import annotations

import math
from typing import Any, Sequence

from . import config
from .tokenizer import tokenize as jieba_tokenize

# 缓存动态构造的 BM25 检索器类（继承 BaseRetriever，需 llama_index 已安装）
_BM25_CLS: type | None = None


def _get_bm25_class() -> type:
    """惰性构造并缓存 JiebaBM25Retriever（继承自 LlamaIndex BaseRetriever）。

    在函数内定义类，既避免模块顶层强依赖 llama_index，又保证单一继承、MRO 正确
    （__init__ / _retrieve 都在本类，super().__init__() 直达 BaseRetriever）。
    """
    global _BM25_CLS
    if _BM25_CLS is not None:
        return _BM25_CLS

    from llama_index.core.base.base_retriever import BaseRetriever
    from llama_index.core.schema import NodeWithScore

    class JiebaBM25Retriever(BaseRetriever):
        """经典 Okapi BM25 关键词检索器（中文用 jieba 分词）。

        打分公式：
            score(D,Q) = Σ_{t∈Q} IDF(t) · f(t,D)·(k1+1) / (f(t,D) + k1·(1 - b + b·|D|/avgdl))
            IDF(t)     = ln(1 + (N - df(t) + 0.5) / (df(t) + 0.5))
        """

        def __init__(
            self,
            nodes: Sequence[Any],
            similarity_top_k: int = 5,
            k1: float = 1.5,
            b: float = 0.75,
            **kwargs: Any,
        ) -> None:
            super().__init__(**kwargs)
            self._nodes = list(nodes)
            self._similarity_top_k = similarity_top_k
            self._k1 = k1
            self._b = b

            self._doc_tf: list[dict[str, int]] = []
            self._doc_len: list[int] = []
            self._df: dict[str, int] = {}
            for n in self._nodes:
                toks = jieba_tokenize(n.get_content())
                tf: dict[str, int] = {}
                for t in toks:
                    tf[t] = tf.get(t, 0) + 1
                self._doc_tf.append(tf)
                self._doc_len.append(len(toks))
                for t in set(toks):
                    self._df[t] = self._df.get(t, 0) + 1
            self._N = len(self._nodes)
            self._avgdl = (sum(self._doc_len) / self._N) if self._N else 0.0

        def _idf(self, term: str) -> float:
            df = self._df.get(term, 0)
            return math.log(1.0 + (self._N - df + 0.5) / (df + 0.5))

        def _score_doc(self, tf: dict[str, int], dl: int, query_terms: set[str]) -> float:
            if self._avgdl <= 0:
                return 0.0
            s = 0.0
            for t in query_terms:
                f = tf.get(t, 0)
                if f == 0:
                    continue
                denom = f + self._k1 * (1.0 - self._b + self._b * dl / self._avgdl)
                s += self._idf(t) * (f * (self._k1 + 1.0)) / denom
            return s

        def _retrieve(self, query_bundle: Any) -> list[Any]:
            q_tokens = set(jieba_tokenize(query_bundle.query_str))
            scored: list[tuple[float, int]] = []
            for i, tf in enumerate(self._doc_tf):
                scored.append((self._score_doc(tf, self._doc_len[i], q_tokens), i))
            # 分数降序；同分按原顺序（确定性）
            scored.sort(key=lambda x: (-x[0], x[1]))
            out: list[Any] = []
            for sc, i in scored[: self._similarity_top_k]:
                if sc <= 0.0:
                    continue  # BM25 只返回有词面命中的文档
                out.append(NodeWithScore(node=self._nodes[i], score=sc))
            return out

    _BM25_CLS = JiebaBM25Retriever
    return _BM25_CLS


def build_bm25_retriever(nodes: Sequence[Any], top_k: int | None = None):
    """构造中文 BM25 检索器（LlamaIndex BaseRetriever 子类）。"""
    cls = _get_bm25_class()
    return cls(nodes=nodes, similarity_top_k=top_k or config.TOP_K)


# --------------------------------------------------------------------------- #
# 向量检索 & RRF 混合
# --------------------------------------------------------------------------- #
def build_vector_retriever(index: Any, top_k: int | None = None):
    return index.as_retriever(similarity_top_k=top_k or config.TOP_K)


def build_hybrid_retriever(vector_retriever: Any, bm25_retriever: Any, top_k: int | None = None):
    """用 QueryFusionRetriever 做 RRF（reciprocal_rerank）融合两路检索。"""
    from llama_index.core.llms.mock import MockLLM
    from llama_index.core.retrievers import QueryFusionRetriever

    return QueryFusionRetriever(
        retrievers=[vector_retriever, bm25_retriever],
        similarity_top_k=top_k or config.TOP_K,
        num_queries=1,             # 只用原始问题，不做 LLM 查询改写
        mode="reciprocal_rerank",  # RRF 倒数排名融合
        # num_queries=1 时 LLM 永不被调用（无查询改写）；但 QueryFusionRetriever 构造期
        # 仍会解析 Settings.llm，默认会去连 OpenAI 并因缺 OPENAI_API_KEY 报错。
        # 这里显式传入离线 MockLLM 占位，规避对 OpenAI 的隐式依赖（真正的回答生成走 answering + 本地 llama.cpp）。
        llm=MockLLM(),
    )


def retrieve(retriever: Any, query: str) -> list[Any]:
    return retriever.retrieve(query)


def format_hits(nodes_with_score: Sequence[Any], top_k: int | None = None) -> list[dict]:
    """把检索结果整理成便于打印/落盘的字典列表（含排名、分数、来源、正文）。"""
    top_k = top_k or config.TOP_K
    hits: list[dict] = []
    for rank, nws in enumerate(list(nodes_with_score)[:top_k], start=1):
        node = getattr(nws, "node", nws)
        meta = node.metadata or {}
        score = getattr(nws, "score", None)
        hits.append(
            {
                "rank": rank,
                "score": float(score) if isinstance(score, (int, float)) else None,
                "source": str(meta.get("source", "未知来源")),
                "chunk_index": meta.get("chunk_index"),
                "node_id": node.node_id,
                "text": node.get_content().strip(),
            }
        )
    return hits
