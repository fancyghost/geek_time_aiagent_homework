"""生成回答用的 LLM：本地 llama.cpp（OpenAI 兼容接口，无需 API Key）。

- 用 llama-index 的 OpenAILike 指向 http://localhost:8080/v1。
- 提供 is_available()/wait_until_available()：在跑"生成带来源回答"前先探活，
  未启动时给出清晰指引，而不是抛一坨连接异常。
"""
from __future__ import annotations

import json
import socket
import urllib.request
from functools import lru_cache
from urllib.parse import urlparse

from . import config


def _models_url() -> str:
    base = config.LLAMA_BASE_URL.rstrip("/")
    return f"{base}/models"


def is_available(timeout: float = 3.0) -> bool:
    """探测本地 llama.cpp /v1/models 是否可达（TCP + HTTP 双重确认）。"""
    parsed = urlparse(config.LLAMA_BASE_URL)
    host = parsed.hostname or "localhost"
    port = parsed.port or 80
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError:
        return False
    try:
        req = urllib.request.Request(_models_url(), headers={"Authorization": f"Bearer {config.LLAMA_API_KEY}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 500
    except Exception:
        # TCP 通但 HTTP 异常，仍认为服务在（可能路径差异），交给真正的调用去报错
        return True


def wait_until_available(timeout: float = 5.0) -> bool:
    return is_available(timeout=timeout)


@lru_cache(maxsize=1)
def get_llm():
    """构造并缓存指向本地 llama.cpp 的 OpenAILike LLM。"""
    from llama_index.llms.openai_like import OpenAILike

    return OpenAILike(
        model=config.LLAMA_MODEL_NAME,
        api_base=config.LLAMA_BASE_URL,
        api_key=config.LLAMA_API_KEY,
        is_chat_model=True,
        is_function_calling_model=False,
        temperature=config.LLM_TEMPERATURE,
        timeout=config.LLM_TIMEOUT,
        context_window=config.LLM_CONTEXT_WINDOW,
    )


def server_info() -> dict:
    """返回本地服务的 /v1/models 摘要（用于演示打印，证明连的是真实本地服务）。"""
    try:
        req = urllib.request.Request(_models_url(), headers={"Authorization": f"Bearer {config.LLAMA_API_KEY}"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        models = [m.get("id") for m in data.get("data", [])]
        return {"ok": True, "base_url": config.LLAMA_BASE_URL, "models": models}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "base_url": config.LLAMA_BASE_URL, "error": str(exc)}


if __name__ == "__main__":  # pragma: no cover - 探活入口
    print(json.dumps(server_info(), ensure_ascii=False, indent=2))
