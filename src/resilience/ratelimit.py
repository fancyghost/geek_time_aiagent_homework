"""按模型独立限流：令牌桶算法，每个 model 一个独立桶，超限抛 429 错误码。"""
import threading
import time

from src.config import get_settings
from src.errors import ErrorCode, GatewayError


class TokenBucket:
    """线程安全令牌桶：rate 为每秒补充速率，burst 为桶容量（允许的突发量）。"""

    def __init__(self, rate: float, burst: int) -> None:
        self.rate = rate
        self.capacity = burst
        self._tokens = float(burst)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        """尝试取 1 个令牌；桶空返回 False（调用方应拒绝请求）。"""
        with self._lock:
            now = time.monotonic()
            # 按经过时间补充令牌，不超过桶容量
            self._tokens = min(
                self.capacity, self._tokens + (now - self._updated) * self.rate
            )
            self._updated = now
            if self._tokens >= 1:
                self._tokens -= 1
                return True
            return False


class RateLimiterRegistry:
    """按 model 维度管理独立令牌桶。"""

    def __init__(self, rate: float, burst: int) -> None:
        self.rate = rate
        self.burst = burst
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def acquire(self, model: str) -> bool:
        with self._lock:
            bucket = self._buckets.get(model)
            if bucket is None:
                bucket = TokenBucket(self.rate, self.burst)
                self._buckets[model] = bucket
        return bucket.acquire()


def _build_registry() -> RateLimiterRegistry:
    settings = get_settings()
    return RateLimiterRegistry(settings.rate_limit_rps, settings.rate_limit_burst)


_registry: RateLimiterRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> RateLimiterRegistry:
    """全局限流器注册表（懒加载单例）。"""
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = _build_registry()
        return _registry


def check_rate_limit(model: str) -> None:
    """请求入口限流检查：超限抛 LOCAL_RATE_LIMITED（对外 HTTP 429）。"""
    if not get_registry().acquire(model):
        raise GatewayError(
            ErrorCode.LOCAL_RATE_LIMITED,
            f"模型 {model} 请求超出速率限制，请稍后重试",
        )
