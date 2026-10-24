"""tests/conftest.py —— 共享夹具与常量。

关键职责：
1. 把 ``guarded_harness/``（作业代码）与 ``../smolagents/src``（上游源码，只读引用）加入 ``sys.path``；
2. 提供 workspace / sandbox / specs / state / store / audit / gate / checkpoint / completion 等夹具；
3. 提供 ``make_harness`` 工厂夹具，便于端到端测试按需装配 ``GuardedHarness``。

所有测试**离线、确定性**：不联网、不依赖真实 LLM，命令白名单仅允许 pytest / python -c。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# guarded_harness/ 根目录（本文件的上一级）。
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import _bootstrap  # noqa: E402  # 触发 sys.path 注入（含 smolagents/src）

_bootstrap.ensure_paths()  # noqa: E402

from audit import AuditLog  # noqa: E402
from catalog import build_specs  # noqa: E402
from checkpoint import Checkpoint  # noqa: E402
from completion import CompletionChecker  # noqa: E402
from gate import Gate  # noqa: E402
from sandbox import Sandbox, make_default_sandbox  # noqa: E402
from state_store import Budget, RunState, StateStore, TaskKind  # noqa: E402

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
# 命令白名单：pytest 相关 + `python -c`（用于超时/输出边界测试）。
# `python -B -m pytest` 关闭字节码写入，规避“同秒覆写源码后子进程加载陈旧 .pyc”的竞态。
ALLOWED_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("python", "-c"),
    ("python", "-B", "-m", "pytest"),
    ("pytest",),
    ("python", "-m", "pytest"),
)
DEFAULT_TIMEOUT = 20.0

BUGGY_APP = "def add(a, b):\n    return a - b\n"
FIXED_APP = "def add(a, b):\n    return a + b\n"
TEST_APP = "from app import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def make_sandbox(workspace: Path, **overrides) -> Sandbox:
    """构造带作业白名单的沙箱。"""

    params: dict = {"allowed_command_prefixes": ALLOWED_PREFIXES, "default_timeout_seconds": DEFAULT_TIMEOUT}
    params.update(overrides)
    return make_default_sandbox(workspace, **params)


# --------------------------------------------------------------------------- #
# 基础夹具
# --------------------------------------------------------------------------- #
@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture
def sandbox(workspace: Path) -> Sandbox:
    return make_sandbox(workspace)


@pytest.fixture
def specs():
    return build_specs()


@pytest.fixture
def state(workspace: Path) -> RunState:
    return RunState(
        run_id="test-run",
        task="",
        task_kind=TaskKind.CODE_UNDERSTANDING,
        workspace_root=str(workspace),
        budget=Budget(),
    )


@pytest.fixture
def store(state: RunState) -> StateStore:
    return StateStore(state)


@pytest.fixture
def audit(tmp_path: Path) -> AuditLog:
    return AuditLog(path=tmp_path / "audit.jsonl")


@pytest.fixture
def gate(specs, sandbox: Sandbox, store: StateStore, audit: AuditLog) -> Gate:
    return Gate(specs, sandbox, store, audit)


@pytest.fixture
def checkpoint(tmp_path: Path, sandbox: Sandbox) -> Checkpoint:
    return Checkpoint(tmp_path / "ck", fingerprint_fn=sandbox.workspace_fingerprint)


@pytest.fixture
def completion() -> CompletionChecker:
    return CompletionChecker()


@pytest.fixture
def app_workspace(workspace: Path) -> Path:
    """带一个 buggy app.py 与 test_app.py 的工作区（测试修复场景）。"""

    (workspace / "app.py").write_text(BUGGY_APP, encoding="utf-8")
    (workspace / "test_app.py").write_text(TEST_APP, encoding="utf-8")
    return workspace


# --------------------------------------------------------------------------- #
# make_harness 工厂
# --------------------------------------------------------------------------- #
@pytest.fixture
def make_harness(workspace: Path, tmp_path: Path):
    """按需装配 ``GuardedHarness``（默认离线沙箱 + FakeModel）。"""

    from fake_model import FakeModel
    from harness import GuardedHarness

    def _make(
        actions=None,
        *,
        model=None,
        task_kind: TaskKind | str = TaskKind.CODE_UNDERSTANDING,
        run_id: str | None = None,
        budget: Budget | None = None,
        sandbox: Sandbox | None = None,
        checkpoint_dir=None,
        audit_path=None,
        duplicate_threshold: int = 3,
    ) -> GuardedHarness:
        sb = sandbox or make_sandbox(workspace)
        mdl = model if model is not None else FakeModel(list(actions or []))
        return GuardedHarness(
            workspace_root=workspace,
            model=mdl,
            task_kind=task_kind,
            run_id=run_id,
            budget=budget,
            sandbox=sb,
            checkpoint_dir=checkpoint_dir if checkpoint_dir is not None else (tmp_path / "ck"),
            audit_path=audit_path if audit_path is not None else (tmp_path / "audit.jsonl"),
            duplicate_threshold=duplicate_threshold,
        )

    return _make
