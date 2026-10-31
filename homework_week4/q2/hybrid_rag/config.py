"""q2 混合检索 RAG · 全局配置。

集中管理：路径、Embedding 模型、本地 llama.cpp LLM 端点、检索超参、以及
"更新验证"用到的有效期常量。所有可切换项都支持环境变量覆盖，默认值来自 .env。

设计要点：
  * Embedding 走**本地 HuggingFace 真实模型** BAAI/bge-large-zh-v1.5，
    经 hf-mirror 下载后完全离线运行（可复现）。
  * 生成回答的 LLM 走**本地 llama.cpp**（OpenAI 兼容，无需 API Key）。
  * 只读资料 login_docs/ 与可变副本 .rag_workspace/login_docs/ 分离，
    演示先拷贝再改动，保证可反复运行、结果一致（与第一题 demo_project 同构）。
"""
from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------- #
# 载入 .env（依次尝试 q2/.env、仓库根 .env；后加载的不覆盖已存在的变量）
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent.parent   # .../homework_week4/q2（config.py 在 hybrid_rag/ 内，需上溯两级）
REPO_ROOT = BASE_DIR.parent.parent                  # .../db_agent


def _load_dotenv() -> None:
    """尽力用 python-dotenv 载入 .env；不可用时静默跳过（默认值兜底）。"""
    try:
        from dotenv import load_dotenv
    except Exception:
        return
    for env_path in (BASE_DIR / ".env", REPO_ROOT / ".env"):
        if env_path.exists():
            load_dotenv(env_path, override=False)


_load_dotenv()

# hf-mirror：让 HuggingFace 模型走国内可达镜像下载（必须在导入 sentence_transformers 前设置）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
# 关闭 HF 联网探测，命中本地缓存即用，避免离线时的超时等待
os.environ.setdefault("HF_HUB_OFFLINE", "0")

# --------------------------------------------------------------------------- #
# 路径常量
# --------------------------------------------------------------------------- #
PRISTINE_DOCS = BASE_DIR / "login_docs"                 # 只读初始资料（提交进仓库）
WORKSPACE = BASE_DIR / ".rag_workspace"                  # 运行生成的可变工作区
WORK_DOCS = WORKSPACE / "login_docs"                     # 演示实际操作/改写的资料副本
INDEX_DIR = BASE_DIR / "index_store"                     # 【运行生成】保存的向量索引 + docstore
ARTIFACTS = BASE_DIR / "artifacts"                       # 【运行生成】三步产物 + 干净转录
RUN_DEMO = BASE_DIR / "run_demo.py"

# --------------------------------------------------------------------------- #
# Embedding（本地 HuggingFace 真实模型）
# --------------------------------------------------------------------------- #
EMBED_MODEL_NAME = os.environ.get("RAG_EMBED_MODEL", "BAAI/bge-large-zh-v1.5")
# bge 系列官方建议：检索时对"查询"加指令前缀，对"文档"不加，可显著提升召回质量
EMBED_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："

# --------------------------------------------------------------------------- #
# LLM（本地 llama.cpp，OpenAI 兼容接口，无需 API Key）
# --------------------------------------------------------------------------- #
LLAMA_BASE_URL = os.environ.get("LLAMA_BASE_URL", "http://localhost:8080/v1")
LLAMA_MODEL_NAME = os.environ.get("LLAMA_MODEL_NAME", "Qwen3.8-27B-NVFP4-MTP-HIGHEST")
LLAMA_API_KEY = os.environ.get("LLAMA_API_KEY", "llama-cpp")   # 占位；llama.cpp 不校验
LLM_TEMPERATURE = float(os.environ.get("RAG_LLM_TEMPERATURE", "0.0"))
LLM_TIMEOUT = float(os.environ.get("RAG_LLM_TIMEOUT", "180"))
LLM_CONTEXT_WINDOW = int(os.environ.get("RAG_LLM_CONTEXT_WINDOW", "8192"))

# --------------------------------------------------------------------------- #
# 分块与检索超参
# --------------------------------------------------------------------------- #
CHUNK_SIZE = int(os.environ.get("RAG_CHUNK_SIZE", "256"))
CHUNK_OVERLAP = int(os.environ.get("RAG_CHUNK_OVERLAP", "48"))
TOP_K = int(os.environ.get("RAG_TOP_K", "5"))

# 演示用的固定问题（作业指定）
QUESTION = "登录会话的有效期是多少分钟？"

# --------------------------------------------------------------------------- #
# "更新验证"用到的有效期（分钟）
# --------------------------------------------------------------------------- #
ORIGINAL_VALIDITY_MINUTES = 30      # 只读资料里的原始有效期
UPDATED_VALIDITY_MINUTES = 20       # 第 3 步在副本中改写成的新有效期
# 资料里表达"有效期分钟数"的确切文本（改写时做精确、无歧义的替换）
VALIDITY_PHRASE_TEMPLATE = "{minutes} 分钟"


def original_validity_phrase() -> str:
    return VALIDITY_PHRASE_TEMPLATE.format(minutes=ORIGINAL_VALIDITY_MINUTES)


def updated_validity_phrase() -> str:
    return VALIDITY_PHRASE_TEMPLATE.format(minutes=UPDATED_VALIDITY_MINUTES)
