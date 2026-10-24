"""checkpoint.py —— 安全边界的保存 / 恢复 / 对账（C1–C4）。

职责（对应交付物 checkpoint.py）：
- **原子保存**：``mkstemp + fsync + os.replace``，任何时刻磁盘上都是完整状态，绝无半截文件；
- **恢复**：``load(run_id)`` / ``load_latest()`` 把 ``RunState`` 无损读回（含预算计数，C3）；
- **对账 ``reconcile``**（恢复时调用）：
  - C2：扫描“写意图已登记但结果未知（PENDING）”的步骤 → ``unknown_result_tool_calls``，
    上层据此**禁止自动重放**；
  - C4：比较当前工作区指纹与保存时指纹 → 若变化则**使旧证据失效、吊销未用审批**。

本模块是纯 Python、无 smolagents 依赖。
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from state_store import ResultStatus, RunState, StateStore

_LATEST = "_latest.json"


@dataclass(slots=True)
class RecoveryReport:
    """一次恢复对账的结论（供审计与上层决策）。"""

    run_id: str
    resumed_from_step: int = 0
    unknown_result_tool_calls: list[str] = field(default_factory=list)
    workspace_changed: bool = False
    invalidated_evidence: int = 0
    revoked_approvals: int = 0
    status: str = ""
    stop_reason: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "resumed_from_step": self.resumed_from_step,
            "unknown_result_tool_calls": list(self.unknown_result_tool_calls),
            "workspace_changed": self.workspace_changed,
            "invalidated_evidence": self.invalidated_evidence,
            "revoked_approvals": self.revoked_approvals,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "notes": list(self.notes),
        }


class Checkpoint:
    """把 ``RunState`` 原子落盘，并支持跨进程恢复与对账。"""

    def __init__(self, directory: str | Path, *,
                 fingerprint_fn: Callable[[], str] | None = None) -> None:
        self.dir = Path(directory)
        self._fingerprint_fn = fingerprint_fn

    # ------------------------------------------------------------------ #
    # 保存（C1：每个安全步骤后调用）
    # ------------------------------------------------------------------ #
    def _path_for(self, run_id: str) -> Path:
        return self.dir / f"{run_id}.json"

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def save(self, state: RunState) -> Path:
        """原子保存状态；保存前把当前工作区指纹写入状态，供恢复时对账（C4）。"""

        self.dir.mkdir(parents=True, exist_ok=True)
        if self._fingerprint_fn is not None:
            try:
                state.workspace_fingerprint = self._fingerprint_fn()
            except Exception:  # noqa: BLE001 - 指纹失败不阻断保存
                pass
        target = self._path_for(state.run_id)
        self._atomic_write(target, json.dumps(state.to_dict(), ensure_ascii=False, indent=2))
        # 更新“最近一次运行”指针，供 load_latest 使用（跨进程共享）。
        self._atomic_write(self.dir / _LATEST, json.dumps({"run_id": state.run_id}, ensure_ascii=False))
        return target

    # ------------------------------------------------------------------ #
    # 恢复（C3：计数不清零）
    # ------------------------------------------------------------------ #
    def load(self, run_id: str) -> RunState | None:
        path = self._path_for(run_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return RunState.from_dict(data)

    def load_latest(self) -> RunState | None:
        pointer = self.dir / _LATEST
        if pointer.exists():
            try:
                run_id = json.loads(pointer.read_text(encoding="utf-8")).get("run_id")
            except json.JSONDecodeError:
                run_id = None
            if run_id:
                loaded = self.load(run_id)
                if loaded is not None:
                    return loaded
        # 回退：选取最近修改的快照。
        candidates = [p for p in self.dir.glob("*.json") if p.name != _LATEST]
        if not candidates:
            return None
        latest = max(candidates, key=lambda p: p.stat().st_mtime)
        return RunState.from_dict(json.loads(latest.read_text(encoding="utf-8")))

    def exists(self, run_id: str) -> bool:
        return self._path_for(run_id).exists()

    # ------------------------------------------------------------------ #
    # 对账（C2 / C4）
    # ------------------------------------------------------------------ #
    def reconcile(self, state: RunState) -> RecoveryReport:
        """恢复时对账：识别结果未知的写入（C2）与工作区变化（C4）。"""

        notes: list[str] = []

        # C4：工作区指纹变化 → 旧证据失效、未用审批吊销。
        current_fp: str | None = None
        if self._fingerprint_fn is not None:
            try:
                current_fp = self._fingerprint_fn()
            except Exception:  # noqa: BLE001
                current_fp = None
        saved_fp = state.workspace_fingerprint
        workspace_changed = bool(current_fp and saved_fp and current_fp != saved_fp)

        invalidated = 0
        revoked = 0
        if workspace_changed:
            store = StateStore(state)
            invalidated = store.invalidate_all_evidence("workspace_changed")
            revoked = store.revoke_approvals("workspace_changed")
            state.workspace_fingerprint = current_fp
            notes.append("workspace_fingerprint_changed")

        # C2：写意图已登记但结果未知（PENDING）→ 禁止自动重放。
        unknown = [
            step.tool_call_id
            for step in state.steps
            if step.is_write and str(step.result_status) == str(ResultStatus.PENDING)
        ]
        if unknown:
            notes.append(f"unknown_result_writes={len(unknown)}")

        return RecoveryReport(
            run_id=state.run_id,
            resumed_from_step=state.budget.steps_used,
            unknown_result_tool_calls=unknown,
            workspace_changed=workspace_changed,
            invalidated_evidence=invalidated,
            revoked_approvals=revoked,
            status=str(state.status),
            stop_reason=str(state.stop_reason) if state.stop_reason is not None else None,
            notes=notes,
        )
