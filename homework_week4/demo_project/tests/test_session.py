"""会话（session）单元测试 —— 初始版本。

这是 homework_week4 记忆演示所指向的「当前项目的会话测试文件」。
它自带一个极简的会话实现（不依赖项目其它模块），因此可独立运行：

    python -m pytest demo_project/tests/test_session.py -q

配套的「测试说明」见项目根目录的 SESSION_TEST.md。
"""
from __future__ import annotations

import time
import uuid

import pytest


# --------------------------------------------------------------------------- #
# 被测的极简会话实现（自包含，便于独立跑通）
# --------------------------------------------------------------------------- #
class SessionStore:
    """内存态会话存储：支持创建、校验、过期与登出。"""

    def __init__(self, ttl_seconds: float = 60.0) -> None:
        self.ttl = ttl_seconds
        self._sessions: dict[str, dict] = {}

    def create(self, user_id: str) -> str:
        token = uuid.uuid4().hex
        self._sessions[token] = {"user_id": user_id, "created_at": time.time()}
        return token

    def is_valid(self, token: str, *, now: float | None = None) -> bool:
        sess = self._sessions.get(token)
        if sess is None:
            return False
        now = time.time() if now is None else now
        return (now - sess["created_at"]) <= self.ttl

    def logout(self, token: str) -> bool:
        return self._sessions.pop(token, None) is not None


# --------------------------------------------------------------------------- #
# 测试用例
# --------------------------------------------------------------------------- #
def test_create_session_is_valid():
    store = SessionStore(ttl_seconds=60)
    token = store.create("user_1")
    assert store.is_valid(token)


def test_session_expires_after_ttl():
    store = SessionStore(ttl_seconds=10)
    token = store.create("user_1")
    created = store._sessions[token]["created_at"]
    # 用固定的 now 避免依赖真实 sleep，保证确定性
    assert not store.is_valid(token, now=created + 11)


def test_logout_invalidates_session():
    store = SessionStore(ttl_seconds=60)
    token = store.create("user_1")
    assert store.logout(token) is True
    assert not store.is_valid(token)


def test_unknown_token_is_invalid():
    store = SessionStore()
    assert not store.is_valid("does-not-exist")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
