"""可观测性存储：SQLite 记录每次调用的 Token 消耗（含分类）与延迟（含 TTFT）。

表 call_records 字段：request_id / model / template_ref / stream / status /
error_code / retries / latency_ms / ttft_ms / input_tokens / output_tokens /
cached_tokens / reasoning_tokens / created_at
"""
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from src.config import get_settings


@dataclass
class CallRecord:
    """单次调用的观测记录。"""
    request_id: str
    model: str
    template_ref: str | None = None
    stream: bool = False
    status: str = "ok"                    # ok / error
    error_code: str | None = None
    retries: int = 0                      # 实际发生的重试次数
    latency_ms: float | None = None       # 总延迟
    ttft_ms: float | None = None          # 首 Token 延迟（仅流式）
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0                # 分类统计：缓存命中 token
    reasoning_tokens: int = 0             # 分类统计：思维链 token
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_row(self) -> tuple:
        return (
            self.request_id, self.model, self.template_ref, int(self.stream),
            self.status, self.error_code, self.retries, self.latency_ms,
            self.ttft_ms, self.input_tokens, self.output_tokens,
            self.cached_tokens, self.reasoning_tokens, self.created_at,
        )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS call_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    model TEXT NOT NULL,
    template_ref TEXT,
    stream INTEGER NOT NULL,
    status TEXT NOT NULL,
    error_code TEXT,
    retries INTEGER NOT NULL DEFAULT 0,
    latency_ms REAL,
    ttft_ms REAL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_call_records_model ON call_records(model);
"""


class ObservabilityStore:
    """SQLite 观测存储：线程安全（每操作独立连接）。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        settings = get_settings()
        path = Path(db_path) if db_path else Path(settings.observability_db)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[2] / path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(path)
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA)

    def record(self, rec: CallRecord) -> None:
        """落盘一条调用记录。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO call_records (
                    request_id, model, template_ref, stream, status, error_code,
                    retries, latency_ms, ttft_ms, input_tokens, output_tokens,
                    cached_tokens, reasoning_tokens, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rec.to_row(),
            )

    def recent_calls(self, limit: int = 50) -> list[dict]:
        """按时间倒序取最近调用明细。"""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM call_records ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def summary(self) -> dict:
        """按模型聚合：调用数、成功率、Token 分类合计、平均/P95 延迟与 TTFT。"""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """SELECT model,
                          COUNT(*) AS calls,
                          SUM(status = 'ok') AS ok_calls,
                          SUM(input_tokens) AS input_tokens,
                          SUM(output_tokens) AS output_tokens,
                          SUM(cached_tokens) AS cached_tokens,
                          SUM(reasoning_tokens) AS reasoning_tokens
                   FROM call_records GROUP BY model"""
            ).fetchall()
            # 按模型分组取延迟与 TTFT 序列（计算 avg/p95）
            lat_rows = conn.execute(
                "SELECT model, latency_ms, ttft_ms FROM call_records"
            ).fetchall()

        latency_by_model: dict[str, list[float]] = {}
        ttft_by_model: dict[str, list[float]] = {}
        for row in lat_rows:
            if row["latency_ms"] is not None:
                latency_by_model.setdefault(row["model"], []).append(row["latency_ms"])
            if row["ttft_ms"] is not None:
                ttft_by_model.setdefault(row["model"], []).append(row["ttft_ms"])

        models = []
        for row in rows:
            model = row["model"]
            lat = latency_by_model.get(model, [])
            ttft = ttft_by_model.get(model, [])
            models.append(
                {
                    "model": model,
                    "calls": row["calls"],
                    "ok_calls": row["ok_calls"],
                    "tokens": {
                        "input": row["input_tokens"],
                        "output": row["output_tokens"],
                        "cached": row["cached_tokens"],
                        "reasoning": row["reasoning_tokens"],
                    },
                    "latency_ms": _stats(lat),
                    "ttft_ms": _stats(ttft),
                }
            )
        return {"models": models}


def _stats(values: list[float]) -> dict | None:
    """计算平均值与 P95；空序列返回 None。"""
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(len(ordered) * 0.95))
    return {
        "avg": round(sum(ordered) / len(ordered), 2),
        "p95": round(ordered[idx], 2),
    }


_store: ObservabilityStore | None = None
_store_lock = threading.Lock()


def get_store() -> ObservabilityStore:
    """全局观测存储（懒加载单例）。"""
    global _store
    with _store_lock:
        if _store is None:
            _store = ObservabilityStore()
        return _store
