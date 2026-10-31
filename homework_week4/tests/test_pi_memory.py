"""pi_memory「项目长期记忆」的 pytest 验证。

覆盖作业要求的四个动作（写入 / 按项目查找并读取 / 更新 / 删除），
并额外验证：跨会话（全新实例）持久化、项目变化后更新旧记录、工具 dispatch 一致性。

运行（在 homework_week4/ 目录下）：
    python -m pytest tests/test_pi_memory.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# 让测试能导入同级的 pi_memory 包（homework_week4/ 加入 sys.path）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pi_memory import (  # noqa: E402
    MEMORY_TOOLS,
    MemoryNotFoundError,
    MemoryStore,
    ProjectMemory,
    dispatch,
    new_memory,
)

PROJECT = "demo_project"


# --------------------------------------------------------------------------- #
# 动作 1：写入 —— 真正落盘
# --------------------------------------------------------------------------- #
def test_write_persists_to_disk(tmp_path):
    mem = new_memory(PROJECT, store_root=tmp_path / "store", project_path=tmp_path)
    record, save_path = mem.write(
        title="会话测试入口",
        content={"test_path": "tests/test_session.py", "run_command": "python -m pytest ..."},
        source=[{"path": "tests/test_session.py", "note": "测试文件"}],
        tags=["session", "pytest"],
    )
    # 真正写入持久化存储：文件存在且内容可读回
    assert save_path.exists()
    on_disk = json.loads(save_path.read_text(encoding="utf-8"))
    assert on_disk["project"] == PROJECT
    assert on_disk["count"] == 1
    assert on_disk["records"][0]["id"] == record.id
    assert on_disk["records"][0]["content"]["test_path"] == "tests/test_session.py"
    assert on_disk["records"][0]["source"][0]["path"] == "tests/test_session.py"


# --------------------------------------------------------------------------- #
# 动作 2：按项目查找并读取
# --------------------------------------------------------------------------- #
def test_find_and_read_by_project(tmp_path):
    store = MemoryStore(tmp_path / "store")
    mem_a = ProjectMemory("proj_a", store)
    mem_b = ProjectMemory("proj_b", store)
    mem_a.write("会话测试入口", {"test_path": "a/tests/test_session.py"}, tags=["session"])
    mem_a.write("部署命令", {"cmd": "make deploy"}, tags=["deploy"])
    mem_b.write("会话测试入口", {"test_path": "b/tests/test_session.py"}, tags=["session"])

    # 按项目隔离：proj_a 只看到自己的 2 条
    assert len(mem_a.read_all()) == 2
    assert len(mem_b.read_all()) == 1

    # 关键词查找
    hits = mem_a.find_and_read(query="会话")
    assert len(hits) == 1 and hits[0].title == "会话测试入口"

    # 标签查找
    assert len(mem_a.find_and_read(tags=["deploy"])) == 1

    # store 层可列出已建库项目
    assert set(store.list_projects()) == {"proj_a", "proj_b"}


# --------------------------------------------------------------------------- #
# 动作 3：更新 —— revision 递增、content 合并、落盘
# --------------------------------------------------------------------------- #
def test_update_bumps_revision_and_merges(tmp_path):
    mem = new_memory(PROJECT, store_root=tmp_path / "store", project_path=tmp_path)
    rec0, _ = mem.write("会话测试入口", {"test_path": "tests/test_session.py", "case_count": 4})

    rec1, save_path = mem.update(
        record_id=rec0.id,
        content={"test_path": "tests/auth/test_session.py", "case_count": 5},
    )
    assert rec1.id == rec0.id                 # 同一条记忆
    assert rec1.revision == rec0.revision + 1  # 修订号递增
    assert rec1.updated_at >= rec0.updated_at
    assert rec1.content["test_path"] == "tests/auth/test_session.py"

    # 落盘后可读回更新后的值
    on_disk = json.loads(save_path.read_text(encoding="utf-8"))
    assert on_disk["records"][0]["revision"] == rec1.revision
    assert on_disk["records"][0]["content"]["case_count"] == 5


def test_update_by_title_when_no_id(tmp_path):
    mem = new_memory(PROJECT, store_root=tmp_path / "store")
    mem.write("会话测试入口", {"test_path": "tests/test_session.py"})
    rec, _ = mem.update(title="会话测试入口", content={"test_path": "tests/auth/test_session.py"})
    assert rec.content["test_path"] == "tests/auth/test_session.py"


# --------------------------------------------------------------------------- #
# 动作 4：删除
# --------------------------------------------------------------------------- #
def test_delete_removes_record(tmp_path):
    mem = new_memory(PROJECT, store_root=tmp_path / "store")
    rec, _ = mem.write("会话测试入口", {"test_path": "tests/test_session.py"})
    assert mem.delete(record_id=rec.id) is True
    assert mem.read_all() == []
    # 再删同一条 -> 定位不到，抛 MemoryNotFoundError
    try:
        mem.delete(record_id=rec.id)
        assert False, "应抛出 MemoryNotFoundError"
    except MemoryNotFoundError:
        pass


# --------------------------------------------------------------------------- #
# 跨会话持久化：全新实例（模拟关闭会话后重开）能读回上一次写入
# --------------------------------------------------------------------------- #
def test_cross_session_persistence(tmp_path):
    store_root = tmp_path / "store"
    # 会话 1：写入
    s1 = new_memory(PROJECT, store_root=store_root, project_path=tmp_path)
    rec, _ = s1.write("会话测试入口", {"test_path": "tests/test_session.py"}, tags=["session"])

    # 会话 2：全新的 MemoryStore + ProjectMemory 实例，只依赖磁盘
    s2 = new_memory(PROJECT, store_root=store_root, project_path=tmp_path)
    hits = s2.find_and_read(query="会话")
    assert len(hits) == 1
    assert hits[0].id == rec.id
    assert hits[0].content["test_path"] == "tests/test_session.py"


# --------------------------------------------------------------------------- #
# 端到端场景：项目变化（迁移测试）后，依据实际结果更新旧记录 + 存在性核对
# --------------------------------------------------------------------------- #
def test_project_change_then_update_and_verify(tmp_path):
    proj_root = tmp_path / "demo_project"
    (proj_root / "tests").mkdir(parents=True)
    (proj_root / "tests" / "test_session.py").write_text("def test_a():\n    assert True\n",
                                                         encoding="utf-8")

    store_root = tmp_path / "store"
    mem = new_memory(PROJECT, store_root=store_root, project_path=proj_root)
    mem.write(
        "会话测试入口",
        {"test_path": "tests/test_session.py", "run_command": "python -m pytest tests/test_session.py -q"},
        source=[{"path": "tests/test_session.py", "note": "测试文件"}],
    )

    # 迁移前：记忆指向的路径确实存在
    before = mem.resolve_path("tests/test_session.py")
    assert before.exists()

    # 制造项目变化：迁移到 tests/auth/test_session.py
    auth_dir = proj_root / "tests" / "auth"
    auth_dir.mkdir(parents=True)
    (auth_dir / "test_session.py").write_text(
        "def test_a():\n    assert True\n\n\ndef test_role():\n    assert True\n",
        encoding="utf-8",
    )
    before.unlink()

    # 依据实际结果更新原来的记忆
    mem.update(
        title="会话测试入口",
        content={
            "test_path": "tests/auth/test_session.py",
            "run_command": "python -m pytest tests/auth/test_session.py -q",
        },
        source=[{"path": "tests/auth/test_session.py", "note": "迁移后的测试文件"}],
    )

    # 全新会话核对：更新后的记忆指向新路径且存在，旧路径不再存在
    mem2 = new_memory(PROJECT, store_root=store_root, project_path=proj_root)
    rec = mem2.find_and_read(query="会话")[0]
    assert rec.content["test_path"] == "tests/auth/test_session.py"
    assert mem2.resolve_path(rec.content["test_path"]).exists()
    assert not mem2.resolve_path("tests/test_session.py").exists()


# --------------------------------------------------------------------------- #
# 工具层：dispatch 与直接调用 ProjectMemory 行为一致
# --------------------------------------------------------------------------- #
def test_tools_dispatch_four_actions(tmp_path):
    store = MemoryStore(tmp_path / "store")
    names = {t["function"]["name"] for t in MEMORY_TOOLS}
    assert names == {"memory_write", "memory_find", "memory_update", "memory_delete"}

    w = dispatch(store, "memory_write", {
        "project": PROJECT, "title": "会话测试入口",
        "content": {"test_path": "tests/test_session.py"},
        "source": ["tests/test_session.py"], "tags": ["session"],
    })
    assert w["ok"] and w["action"] == "write"
    rid = w["record"]["id"]

    f = dispatch(store, "memory_find", {"project": PROJECT, "query": "会话"})
    assert f["ok"] and f["count"] == 1

    u = dispatch(store, "memory_update", {
        "project": PROJECT, "record_id": rid,
        "content": {"test_path": "tests/auth/test_session.py"},
    })
    assert u["ok"] and u["record"]["revision"] == 2
    assert u["record"]["content"]["test_path"] == "tests/auth/test_session.py"

    d = dispatch(store, "memory_delete", {"project": PROJECT, "record_id": rid})
    assert d["ok"] and d["deleted"] is True
    assert dispatch(store, "memory_find", {"project": PROJECT})["count"] == 0


def test_dispatch_error_paths(tmp_path):
    store = MemoryStore(tmp_path / "store")
    # 缺 project
    assert dispatch(store, "memory_write", {"title": "x"})["ok"] is False
    # 更新不存在的记忆
    bad = dispatch(store, "memory_update", {"project": PROJECT, "title": "不存在"})
    assert bad["ok"] is False and "error" in bad
    # 未知工具
    assert dispatch(store, "memory_unknown", {"project": PROJECT})["ok"] is False
