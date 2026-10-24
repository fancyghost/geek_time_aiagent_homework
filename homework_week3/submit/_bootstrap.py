"""_bootstrap.py —— sys.path 引导（不修改 smolagents 源码）。

作业代码位于 ``homework_week3/guarded_harness/``，上游 smolagents 源码位于
``homework_week3/smolagents/``（**全程保持不改动**）。本模块只做一件事：把

1. 本目录（``guarded_harness/``）——让 ``import gate / sandbox / harness ...`` 可用；
2. ``../smolagents/src``——让 ``import smolagents`` 指向上游未修改的源码；

注入 ``sys.path``。导入本模块即自动生效（``ensure_paths()`` 在模块末尾调用一次）。
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent            # guarded_harness/
HOMEWORK = HERE.parent                            # homework_week3/
SMOLAGENTS_SRC = HOMEWORK / "smolagents" / "src"  # 上游源码（只读引用）


def ensure_paths() -> None:
    """把本目录与 smolagents/src 幂等地插入 sys.path 头部。"""

    for path in (str(HERE), str(SMOLAGENTS_SRC)):
        if path not in sys.path:
            sys.path.insert(0, path)


# 导入即生效，保证后续 `from smolagents...` 与 `import gate` 都能解析。
ensure_paths()
