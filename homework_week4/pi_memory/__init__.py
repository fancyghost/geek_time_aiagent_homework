"""pi_memory —— 为 pi（Agent）提供的「项目长期记忆」能力。

对外导出：
  MemoryRecord       记忆条目数据模型
  MemoryStore        低层多项目持久化存储（JSON + 原子落盘）
  ProjectMemory      高层单项目视图：write / find_and_read / update / delete
  MemoryNotFoundError 定位不到记忆时抛出
  MEMORY_TOOLS       四个动作的 OpenAI function-calling 工具 schema
  dispatch           工具分发执行器
  new_memory         便捷工厂：给定项目与存储根，直接拿到 ProjectMemory

典型用法：
    from pi_memory import new_memory
    mem = new_memory("demo_project", store_root="./memory_store",
                     project_path="/abs/path/to/demo_project")
    mem.write("会话测试入口", {"test_path": "tests/test_session.py",
              "run_command": "python -m pytest tests/test_session.py -q"},
              source=[{"path": "tests/test_session.py", "note": "测试文件"}],
              tags=["session", "pytest"])
"""
from __future__ import annotations

import os
from pathlib import Path

from .memory import MemoryNotFoundError, ProjectMemory
from .record import MemoryRecord
from .store import MemoryStore
from .tools import MEMORY_TOOLS, dispatch

__all__ = [
    "MemoryRecord",
    "MemoryStore",
    "ProjectMemory",
    "MemoryNotFoundError",
    "MEMORY_TOOLS",
    "dispatch",
    "new_memory",
]


def new_memory(
    project: str,
    store_root: str | os.PathLike[str] = "./memory_store",
    project_path: str | os.PathLike[str] | None = None,
) -> ProjectMemory:
    """便捷工厂：构造一个绑定到 project 的 ProjectMemory。

    store_root   : 记忆库根目录（所有项目共用）。
    project_path : 该项目根的绝对路径，用于把记忆里的相对路径解析成绝对路径。
    """
    store = MemoryStore(Path(store_root))
    return ProjectMemory(project, store, project_path=project_path)
