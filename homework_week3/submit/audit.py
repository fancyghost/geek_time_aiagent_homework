"""audit.py —— append-only 审计日志（A1 / A2）。

职责（对应交付物 audit.py）：
- 记录每一次放行 / 拒绝 / 审批 / 执行 / 结果 / 状态变更；
- 记录**不可变**（frozen dataclass），序号 ``seq`` 单调递增，只追加不修改；
- 可按 ``run_id``、``tool_call_id`` 还原完整决策链；
- 可选持久化为 JSONL：跨进程追加，进程崩溃也不丢已写入的行。

本模块是纯 Python、无 smolagents 依赖，可独立单测。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class AuditPhase(StrEnum):
    """审计阶段：门禁 / 沙箱 / 审批 / 执行 / 结果 / 状态。"""

    GATE = "gate"
    SANDBOX = "sandbox"
    APPROVAL = "approval"
    EXECUTION = "execution"
    RESULT = "result"
    STATE = "state"


class AuditDecision(StrEnum):
    """审计结论。"""

    ALLOW = "allow"
    DENY = "deny"
    CONFIRM = "confirm"        # 需要人工确认（等待审批）
    APPROVED = "approved"      # 已批准
    EXECUTED = "executed"      # handler 已执行
    FAILED = "failed"          # 执行失败
    RECORDED = "recorded"      # 状态记录


class AuditActor(StrEnum):
    """动作发起者 / 责任方。"""

    MODEL = "model"
    HARNESS = "harness"
    USER = "user"
    SYSTEM = "system"


@dataclass(frozen=True)
class AuditRecord:
    """一条不可变的审计记录（尝试改字段会抛 ``FrozenInstanceError``）。"""

    seq: int
    run_id: str
    phase: str
    decision: str
    rule: str
    actor: str
    tool_call_id: str | None = None
    tool_name: str | None = None
    step_number: int | None = None
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    latency_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "run_id": self.run_id,
            "phase": self.phase,
            "decision": self.decision,
            "rule": self.rule,
            "actor": self.actor,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "step_number": self.step_number,
            "message": self.message,
            "detail": dict(self.detail),
            "latency_ms": self.latency_ms,
        }


class AuditLog:
    """append-only 审计日志（内存列表 + 可选 JSONL 持久化）。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._records: list[AuditRecord] = []
        self._seq = 0
        self._lock = threading.Lock()
        # 若已存在审计文件，续接其最大 seq，保证 append-only 语义跨进程连续。
        if self._path is not None and self._path.exists():
            existing = self.load_jsonl(self._path)
            self._seq = max((int(r.get("seq", 0)) for r in existing), default=0)

    # ---- 写入（只追加）----
    def append(
        self,
        *,
        run_id: str,
        phase: AuditPhase | str,
        decision: AuditDecision | str,
        rule: str,
        actor: AuditActor | str = AuditActor.HARNESS,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        step_number: int | None = None,
        message: str = "",
        detail: dict[str, Any] | None = None,
        latency_ms: float | None = None,
    ) -> AuditRecord:
        with self._lock:
            self._seq += 1
            record = AuditRecord(
                seq=self._seq,
                run_id=run_id,
                phase=str(phase),
                decision=str(decision),
                rule=rule,
                actor=str(actor),
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                step_number=step_number,
                message=message,
                detail=dict(detail or {}),
                latency_ms=latency_ms,
            )
            self._records.append(record)
        if self._path is not None:
            # 追加写；即使进程崩溃，已写入的行也不会丢失或被改写。
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
        return record

    # ---- 查询（只读）----
    @property
    def records(self) -> list[AuditRecord]:
        return list(self._records)

    def __len__(self) -> int:
        return len(self._records)

    def by_run(self, run_id: str) -> list[AuditRecord]:
        return [r for r in self._records if r.run_id == run_id]

    def by_tool_call(self, tool_call_id: str) -> list[AuditRecord]:
        return [r for r in self._records if r.tool_call_id == tool_call_id]

    def chain(self, tool_call_id: str) -> list[AuditRecord]:
        """还原某个 tool_call_id 的完整决策链（按 seq 升序）。"""

        return sorted(self.by_tool_call(tool_call_id), key=lambda r: r.seq)

    def find(self, *, run_id: str | None = None, phase: str | None = None,
             decision: str | None = None, rule: str | None = None) -> list[AuditRecord]:
        def match(r: AuditRecord) -> bool:
            return (
                (run_id is None or r.run_id == run_id)
                and (phase is None or r.phase == phase)
                and (decision is None or r.decision == decision)
                and (rule is None or r.rule == rule)
            )

        return [r for r in self._records if match(r)]

    @staticmethod
    def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
        """从 JSONL 文件读回审计记录（用于跨进程还原决策链）。"""

        records: list[dict[str, Any]] = []
        with Path(path).open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records

    def render_chain(self, records: Iterable[AuditRecord]) -> str:
        """把决策链渲染成可读文本，便于人工核对与交付。"""

        lines: list[str] = []
        for r in records:
            head = f"#{r.seq:>3} [{r.phase}/{r.decision}] rule={r.rule} actor={r.actor}"
            if r.tool_call_id:
                head += f" tool_call_id={r.tool_call_id}"
            if r.tool_name:
                head += f" tool={r.tool_name}"
            if r.message:
                head += f" :: {r.message}"
            lines.append(head)
        return "\n".join(lines)
