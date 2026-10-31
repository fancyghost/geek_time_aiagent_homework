"""持久化存储层：把项目长期记忆真正落到磁盘。

设计要点（对齐课程一贯的"离线确定性 + 原子落盘"原则）：
  * 按项目分文件：<store_root>/projects/<project_key>.json，天然支持"按项目查找并读取"。
  * 原子写：临时文件 + flush + fsync + os.replace，任何时刻磁盘上都是完整 JSON，绝无半截文件。
  * 跨进程/跨会话持久：记忆只依赖磁盘文件，新建 MemoryStore 实例即可读回上一个会话写入的内容。
  * UTF-8 + ensure_ascii=False + indent=2，落盘文件人类可读，便于截图核对。

不依赖任何向量数据库；纯标准库实现。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .record import MemoryRecord


def _slug(project: str) -> str:
    """把项目键规整成安全的文件名片段。"""
    s = re.sub(r"[^0-9A-Za-z_.\-]+", "_", project.strip())
    return s.strip("._") or "default"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """原子写 JSON：先写同目录临时文件并 fsync，再 os.replace 覆盖目标。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        # 失败时清理临时文件，避免留下垃圾
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
        raise


class MemoryStore:
    """多项目长期记忆存储（低层）。

    一个 MemoryStore 绑定一个 store_root 目录；不同项目的记忆分别落在
    projects/<key>.json 中。所有高层动作（写/查/改/删）最终都改这些文件。
    """

    def __init__(self, store_root: str | os.PathLike[str]) -> None:
        self.store_root = Path(store_root).resolve()
        self.projects_dir = self.store_root / "projects"
        self.projects_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # 路径与项目索引
    # ------------------------------------------------------------------ #
    def project_file(self, project: str) -> Path:
        return self.projects_dir / f"{_slug(project)}.json"

    def list_projects(self) -> list[str]:
        """列出已建库的所有项目键。"""
        out: list[str] = []
        for f in sorted(self.projects_dir.glob("*.json")):
            data = self._read_project_file(f)
            if data:
                out.append(data.get("project") or f.stem)
        return out

    def _read_project_file(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    # ------------------------------------------------------------------ #
    # 读：按项目读取全部记忆条目
    # ------------------------------------------------------------------ #
    def read_project(self, project: str) -> tuple[list[MemoryRecord], dict[str, Any]]:
        """读取某项目的全部记忆，返回 (记录列表, 项目元信息)。

        项目元信息含 project / project_path（项目根绝对路径），
        供上层把记忆里的相对路径解析成绝对路径做存在性核对。
        """
        data = self._read_project_file(self.project_file(project))
        if not data:
            return [], {"project": project, "project_path": None, "count": 0}
        records = [MemoryRecord.from_dict(r) for r in data.get("records", [])]
        meta = {
            "project": data.get("project", project),
            "project_path": data.get("project_path"),
            "count": len(records),
        }
        return records, meta

    def project_path(self, project: str) -> str | None:
        _, meta = self.read_project(project)
        return meta.get("project_path")

    def resolve(self, project: str, rel_or_abs: str) -> Path:
        """把记忆里的路径解析成绝对路径：绝对路径原样返回，相对路径拼项目根。"""
        p = Path(rel_or_abs)
        if p.is_absolute():
            return p
        root = self.project_path(project)
        base = Path(root) if root else self.store_root
        return (base / p).resolve()

    # ------------------------------------------------------------------ #
    # 写：整体覆盖某项目的记忆文件（原子）
    # ------------------------------------------------------------------ #
    def _write_project(
        self, project: str, records: list[MemoryRecord], project_path: str | None
    ) -> Path:
        payload = {
            "project": project,
            "project_path": project_path,
            "count": len(records),
            "records": [r.to_dict() for r in records],
        }
        path = self.project_file(project)
        _atomic_write_json(path, payload)
        return path

    def save_record(
        self, record: MemoryRecord, project_path: str | None = None
    ) -> Path:
        """新增一条记忆并落盘；返回实际保存的文件路径。"""
        records, meta = self.read_project(record.project)
        # 保留已有 project_path；本次显式给出则更新
        new_root = project_path or meta.get("project_path")
        records.append(record)
        return self._write_project(record.project, records, new_root)

    def replace_record(
        self, record: MemoryRecord, project_path: str | None = None
    ) -> Path:
        """用同 id 的新版本替换旧记录（用于 update）；返回保存路径。"""
        records, meta = self.read_project(record.project)
        new_root = project_path or meta.get("project_path")
        replaced = False
        for i, r in enumerate(records):
            if r.id == record.id:
                records[i] = record
                replaced = True
                break
        if not replaced:
            records.append(record)
        return self._write_project(record.project, records, new_root)

    def delete_record(self, project: str, record_id: str) -> bool:
        """按 id 删除一条记忆并落盘；返回是否真的删掉了。"""
        records, meta = self.read_project(project)
        kept = [r for r in records if r.id != record_id]
        if len(kept) == len(records):
            return False
        self._write_project(project, kept, meta.get("project_path"))
        return True

    def set_project_path(self, project: str, project_path: str) -> None:
        """登记/更新项目根绝对路径（不改动已有记录）。"""
        records, meta = self.read_project(project)
        self._write_project(project, records, project_path)

    def clear_project(self, project: str) -> None:
        """清空某项目的记忆文件（用于演示前的复位）。"""
        path = self.project_file(project)
        if path.exists():
            os.remove(path)
