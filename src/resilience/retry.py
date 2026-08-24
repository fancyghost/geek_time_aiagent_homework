"""指数退避重试：仅对可重试错误生效，退避序列 0.5s→1s→2s，最多重试 3 次。"""
import time
from typing import Callable, TypeVar

from src.errors import GatewayError, translate_exception

T = TypeVar("T")

# 重试策略常量：最多重试 3 次（首次尝试不计入），退避基数 0.5s 指数翻倍
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 0.5


def with_retry(
    fn: Callable[[int], T],
    *,
    max_retries: int = MAX_RETRIES,
    base_backoff: float = BASE_BACKOFF_SECONDS,
    can_retry: Callable[[], bool] | None = None,
    on_retry: Callable[[int, GatewayError, float], None] | None = None,
) -> T:
    """带指数退避的重试包装。

    fn(attempt)：实际调用，attempt 为 0 起的尝试序号（可用于故障注入）
    can_retry：返回 False 时禁止重试（如流式已开始下发）
    on_retry：每次重试前的回调（attempt, 错误, 退避秒数），用于日志与观测
    """
    attempt = 0
    while True:
        try:
            return fn(attempt)
        except Exception as exc:  # noqa: BLE001 —— 归一为 GatewayError 后判断可重试性
            err = translate_exception(exc)
            if (
                attempt >= max_retries
                or not err.retryable
                or (can_retry is not None and not can_retry())
            ):
                raise err from exc
            backoff = base_backoff * (2 ** attempt)  # 0.5s → 1s → 2s
            if on_retry is not None:
                on_retry(attempt, err, backoff)
            time.sleep(backoff)
            attempt += 1
