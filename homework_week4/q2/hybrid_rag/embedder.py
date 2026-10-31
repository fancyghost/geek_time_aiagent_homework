"""真实 Embedding：本地 HuggingFace BAAI/bge-large-zh-v1.5。

- 首次使用会经 hf-mirror（HF_ENDPOINT）下载模型到本地缓存，之后完全离线、可复现。
- 采用单例缓存，避免同进程重复加载 ~1.3GB 模型。
- bge 系列建议：对"查询"加检索指令前缀（query_instruction），对"文档"不加，
  以匹配官方训练方式，提升中文检索召回质量。

可用 `python -m hybrid_rag.embedder` 预先下载/自检（打印一条向量维度）。
"""
from __future__ import annotations

from functools import lru_cache

from . import config


@lru_cache(maxsize=1)
def get_embed_model():
    """构造并缓存 HuggingFaceEmbedding 单例。"""
    from llama_index.embeddings.huggingface import HuggingFaceEmbedding

    # 说明：config 已在导入时设置 HF_ENDPOINT=hf-mirror，确保国内可下载。
    return HuggingFaceEmbedding(
        model_name=config.EMBED_MODEL_NAME,
        query_instruction=config.EMBED_QUERY_INSTRUCTION,
    )


def embed_dim() -> int:
    """返回向量维度（bge-large-zh-v1.5 为 1024）。"""
    vec = get_embed_model().get_text_embedding("维度自检")
    return len(vec)


if __name__ == "__main__":  # pragma: no cover - 预下载/自检入口
    import os
    import time

    print(f"[embed] HF_ENDPOINT = {os.environ.get('HF_ENDPOINT', '(default)')}")
    print(f"[embed] 载入模型 {config.EMBED_MODEL_NAME} …（首次会下载约 1.3GB）")
    t0 = time.time()
    dim = embed_dim()
    print(f"[embed] 就绪：向量维度 = {dim}，用时 {time.time() - t0:.1f}s")
