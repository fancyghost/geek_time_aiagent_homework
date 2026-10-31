"""高层记忆 API：ProjectMemory —— pi 用来管理"某个项目"长期记忆的入口。

四个动作（严格对应作业要求）：
  1. 写入          write(title, content, source, tags)
  2. 按项目查找并读取 find_and_read(query, tags) / read_all()
  3. 更新          update(record_id | title, content=..., source=..., tags=..., title=...)
  4. 删除          delete(record_id | title)

ProjectMemory 绑定一个 project 键与一个 MemoryStore；所有动作都真正落盘，
因此"关闭会话、启动全新会话"后用同样的 project 键重新构造 ProjectMemory，
即可读回上一个会话写入的记忆（跨会话持久化）。
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from .record import MemoryRecord
from .store import MemoryStore


class MemoryNotFoundError(KeyError):
    """按 id / title 定位记忆但没找到时抛出。"""


class ProjectMemory:
    """绑定到单个项目的长期记忆视图。"""

    def __init__(
        self,
        project: str,
        store: MemoryStore,
        project_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.project = project
        self.store = store
        # 登记项目根路径，供相对路径解析与存在性核对
        if project_path is not None:
            store.set_project_path(project, str(Path(project_path).resolve()))

    # ------------------------------------------------------------------ #
    # 动作 1：写入
    # ------------------------------------------------------------------ #
    def write(
        self,
        title: str,
        content: dict[str, Any] | None = None,
        source: list[dict[str, str]] | list[str] | None = None,
        tags: list[str] | None = None,
    ) -> tuple[MemoryRecord, Path]:
        """写入一条新记忆并落盘。返回 (记录, 实际保存路径)。

        source 允许两种写法：字符串列表 或 {"path","note"} 字典列表，内部统一成字典。
        """
        record = MemoryRecord(
            project=self.project,
            title=title,
            content=dict(content or {}),
            source=_normalize_source(source),
            tags=list(tags or []),
        )
        save_path = self.store.save_record(record)
        return record, save_path

    # ------------------------------------------------------------------ #
    # 动作 2：按项目查找并读取
    # ------------------------------------------------------------------ #
    def read_all(self) -> list[MemoryRecord]:
        """读取本项目全部记忆。"""
        records, _ = self.store.read_project(self.project)
        return records

    def find_and_read(
        self, query: str | None = None, tags: list[str] | None = None
    ) -> list[MemoryRecord]:
        """在本项目内按关键词/标签查找并读取记忆（命中多条时全部返回）。"""
        return [r for r in self.read_all() if r.matches(query=query, tags=tags)]

    def get(self, record_id: str | None = None, title: str | None = None) -> MemoryRecord:
        """精确定位单条记忆：优先按 id，其次按 title（同项目内唯一）。找不到抛 MemoryNotFoundError。"""
        records = self.read_all()
        if record_id is not None:
            for r in records:
                if r.id == record_id:
                    return r
            raise MemoryNotFoundError(f"项目 {self.project} 中不存在 id={record_id} 的记忆")
        if title is not None:
            for r in records:
                if r.title == title:
                    return r
            raise MemoryNotFoundError(f"项目 {self.project} 中不存在 title={title!r} 的记忆")
        raise ValueError("get() 需要 record_id 或 title 至少其一")

    # ------------------------------------------------------------------ #
    # 动作 3：更新
    # ------------------------------------------------------------------ #
    def update(
        self,
        record_id: str | None = None,
        title: str | None = None,
        *,
        new_title: str | None = None,
        content: dict[str, Any] | None = None,
        source: list[dict[str, str]] | list[str] | None = None,
        tags: list[str] | None = None,
    ) -> tuple[MemoryRecord, Path]:
        """更新一条已有记忆并落盘。返回 (新版本记录, 保存路径)。

        定位方式：record_id 优先，否则用 title。
        只有显式传入的字段才会被改；content 采用"合并"语义（旧键保留，新键覆盖）。
        每次更新 revision +1、updated_at 刷新，用以证明"旧记录被真正改过"。
        """
        record = self.get(record_id=record_id, title=title)
        if new_title is not None:
            record.title = new_title
        if content is not None:
            merged = dict(record.content)
            merged.update(content)
            record.content = merged
        if source is not None:
            record.source = _normalize_source(source)
        if tags is not None:
            record.tags = list(tags)
        record.revision += 1
        record.updated_at = time.time()
        save_path = self.store.replace_record(record)
        return record, save_path

    # ------------------------------------------------------------------ #
    # 动作 4：删除
    # ------------------------------------------------------------------ #
    def delete(self, record_id: str | None = None, title: str | None = None) -> bool:
        """删除一条记忆并落盘。返回是否真的删掉了。"""
        record = self.get(record_id=record_id, title=title)
        return self.store.delete_record(self.project, record.id)

    # ------------------------------------------------------------------ #
    # 便捷：把记忆里的相对路径解析为绝对路径并核对是否存在
    # ------------------------------------------------------------------ #
    def resolve_path(self, rel_or_abs: str) -> Path:
        return self.store.resolve(self.project, rel_or_abs)


def _normalize_source(
    source: list[dict[str, str]] | list[str] | None,
) -> list[dict[str, str]]:
    """把来源统一成 [{"path":..., "note":...}] 结构。"""
    if not source:
        return []
    out: list[dict[str, str]] = []
    for item in source:
        if isinstance(item, str):
            out.append({"path": item, "note": ""})
        elif isinstance(item, dict):
            out.append({"path": str(item.get("path", "")), "note": str(item.get("note", ""))})
        else:  # 兜底
            out.append({"path": str(item), "note": ""})
    return out
