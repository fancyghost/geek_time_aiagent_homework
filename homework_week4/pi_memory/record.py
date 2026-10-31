"""记忆条目数据模型。

一条 MemoryRecord 就是「项目长期记忆」里的最小存储单元：
它记录某个项目下的一条事实（如"会话测试入口在哪、怎么跑"），
并带上来源（source）、标签（tags）、创建/更新时间与修订号，
以便后续按项目查找、读取、更新与删除。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


def _now() -> float:
    """统一时间源：Unix 秒（浮点）。"""
    return time.time()


def _iso(ts: float) -> str:
    """把 Unix 秒转成本地可读的 ISO8601 字符串（仅用于展示/落盘）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


@dataclass
class MemoryRecord:
    """一条项目长期记忆。

    字段说明：
      id          : 记录唯一标识（uuid4 hex），一经创建保持不变，用于精确定位更新/删除。
      project     : 所属项目键（如 "demo_project"），"按项目查找" 的依据。
      title       : 人类可读标题（如 "会话测试入口"），同一项目内用于模糊定位。
      content     : 记忆正文，结构化 dict（如 {"test_path":..., "run_command":...}）。
      source      : 来源清单，每项形如 {"path":..., "note":...}，回答"这条记忆从哪来"。
      tags        : 关键词标签，辅助检索。
      created_at  : 创建时间（Unix 秒）。
      updated_at  : 最近更新时间（Unix 秒）。
      revision    : 修订号，每次 update +1，用于证明"旧记录被真正改过"。
    """

    project: str
    title: str
    content: dict[str, Any] = field(default_factory=dict)
    source: list[dict[str, str]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    revision: int = 1

    # ------------------------------------------------------------------ #
    # 序列化：落盘为 JSON（ensure_ascii=False，中文可读）
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project": self.project,
            "title": self.title,
            "content": self.content,
            "source": self.source,
            "tags": self.tags,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "created_at_iso": _iso(self.created_at),
            "updated_at_iso": _iso(self.updated_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryRecord":
        return cls(
            id=data["id"],
            project=data["project"],
            title=data["title"],
            content=dict(data.get("content") or {}),
            source=list(data.get("source") or []),
            tags=list(data.get("tags") or []),
            created_at=float(data.get("created_at") or _now()),
            updated_at=float(data.get("updated_at") or _now()),
            revision=int(data.get("revision") or 1),
        )

    # ------------------------------------------------------------------ #
    # 检索辅助
    # ------------------------------------------------------------------ #
    def matches(self, query: str | None = None, tags: list[str] | None = None) -> bool:
        """判断本记录是否命中检索条件。

        - query 为空且 tags 为空 -> 命中（返回全部）。
        - query 非空 -> 在 title / content 的键值 / tags 里做大小写不敏感子串匹配。
        - tags 非空 -> 记录 tags 与之有交集即命中。
        两者同时给出时取「与」（都满足才命中）。
        """
        ok = True
        if tags:
            wanted = {t.lower() for t in tags}
            owned = {t.lower() for t in self.tags}
            ok = ok and bool(wanted & owned)
        if query:
            q = query.lower()
            haystack = [self.title.lower(), " ".join(self.tags).lower()]
            for k, v in self.content.items():
                haystack.append(str(k).lower())
                haystack.append(str(v).lower())
            ok = ok and any(q in h for h in haystack)
        return ok
