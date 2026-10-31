"""homework_week4 · 第一题演示：为 pi 增加「项目长期记忆」。

本脚本把作业的三步验证编排成一个**离线、确定、可重复运行**的演示：

  步骤 1（建库结果）    : pi 读取"当前项目"的会话测试文件与测试说明，
                          调用 memory_write 工具把「测试路径 + 运行命令 + 来源」写入长期记忆，
                          打印记忆真正落盘的实际保存路径。
  步骤 2（查询对比与回答）: 关闭会话 → 启动**全新进程**（真·新会话，只靠磁盘记忆恢复），
                          调用 memory_find 按项目查找"会话测试入口"，再核对磁盘上文件是否存在，
                          给出"记忆 vs 磁盘"的对比与回答。
  步骤 3（更新后的回答） : 制造一次真实的项目变化——把会话测试迁移到 tests/auth/test_session.py、
                          同步改写测试说明、真正运行新的测试命令，然后调用 memory_update
                          依据实际结果更新原来那条长期记忆，给出更新后的回答。

设计说明：
  * "当前项目"= demo_project/（仓库中提交的初始场景，只读、永不被演示破坏）。
  * 演示在 .demo_workspace/demo_project/ 上操作：每次运行先复位（从只读初始场景整目录拷贝），
    因此可反复运行，结果完全一致。
  * 记忆里保存的是**项目相对路径**（如 tests/test_session.py），与作业叙述一致；
    核对存在性时用 project_path 解析成绝对路径。
  * 步骤 2 的"全新会话"通过**独立子进程**实现，是最强的跨会话持久化证据。

运行（在 homework_week4/ 目录下，PowerShell 用 ; 分隔；建议先 chcp 65001）：
    python run_demo.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

# 允许以 `python run_demo.py` 直接运行时导入同目录的 pi_memory 包
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from pi_memory import MEMORY_TOOLS, MemoryStore, dispatch  # noqa: E402

# --------------------------------------------------------------------------- #
# 路径常量
# --------------------------------------------------------------------------- #
PROJECT_KEY = "demo_project"
PRISTINE_PROJECT = BASE_DIR / "demo_project"            # 只读初始场景（提交进仓库）
WORKSPACE = BASE_DIR / ".demo_workspace"
WORK_PROJECT = WORKSPACE / "demo_project"               # 演示实际操作的可变副本
STORE_ROOT = BASE_DIR / "memory_store"                  # 记忆库根（建库结果落这里）
ARTIFACTS = BASE_DIR / "artifacts"                      # 三步产物（便于截图/复查）
RUN_DEMO = BASE_DIR / "run_demo.py"

# 记忆里保存的项目相对路径
REL_TEST_INITIAL = "tests/test_session.py"
REL_TEST_MIGRATED = "tests/auth/test_session.py"
REL_DOC = "SESSION_TEST.md"
RUN_CMD_INITIAL = f"python -m pytest {REL_TEST_INITIAL} -q"
RUN_CMD_MIGRATED = f"python -m pytest {REL_TEST_MIGRATED} -q"
MEMORY_TITLE = "会话测试入口"


# --------------------------------------------------------------------------- #
# 迁移后的新内容（步骤 3 制造的"项目变化"）
#   —— 会话测试挪到 auth/ 子包，并新增一条"会话记录角色"的用例（4 -> 5）
# --------------------------------------------------------------------------- #
MIGRATED_TEST_PY = '''"""会话（session）单元测试 —— 迁移到 auth 子包后的版本。

项目变化：会话测试从 tests/test_session.py 迁移到 tests/auth/test_session.py，
并新增 test_create_session_records_role（用例数 4 -> 5）。

运行：
    python -m pytest tests/auth/test_session.py -q
"""
from __future__ import annotations

import time
import uuid

import pytest


class SessionStore:
    """内存态会话存储：支持创建（带角色）、校验、过期与登出。"""

    def __init__(self, ttl_seconds: float = 60.0) -> None:
        self.ttl = ttl_seconds
        self._sessions: dict[str, dict] = {}

    def create(self, user_id: str, role: str = "member") -> str:
        token = uuid.uuid4().hex
        self._sessions[token] = {
            "user_id": user_id,
            "role": role,
            "created_at": time.time(),
        }
        return token

    def role_of(self, token: str) -> str | None:
        sess = self._sessions.get(token)
        return sess["role"] if sess else None

    def is_valid(self, token: str, *, now: float | None = None) -> bool:
        sess = self._sessions.get(token)
        if sess is None:
            return False
        now = time.time() if now is None else now
        return (now - sess["created_at"]) <= self.ttl

    def logout(self, token: str) -> bool:
        return self._sessions.pop(token, None) is not None


def test_create_session_is_valid():
    store = SessionStore(ttl_seconds=60)
    token = store.create("user_1")
    assert store.is_valid(token)


def test_create_session_records_role():
    store = SessionStore()
    token = store.create("user_admin", role="admin")
    assert store.role_of(token) == "admin"


def test_session_expires_after_ttl():
    store = SessionStore(ttl_seconds=10)
    token = store.create("user_1")
    created = store._sessions[token]["created_at"]
    assert not store.is_valid(token, now=created + 11)


def test_logout_invalidates_session():
    store = SessionStore(ttl_seconds=60)
    token = store.create("user_1")
    assert store.logout(token) is True
    assert not store.is_valid(token)


def test_unknown_token_is_invalid():
    store = SessionStore()
    assert not store.is_valid("does-not-exist")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
'''

MIGRATED_SESSION_MD = '''# 会话测试说明（SESSION_TEST）· 已更新

> 项目变化：会话测试已迁移到 `tests/auth/` 子包，并新增"会话角色"用例。
> 本说明与长期记忆保持同步（记忆 revision 已随之更新）。

## 测试目标

验证用户会话（session）的核心生命周期：**创建（含角色）→ 校验 → 过期 → 登出**。

## 测试入口

| 项 | 值 |
|---|---|
| 测试文件 | `tests/auth/test_session.py` |
| 运行命令 | `python -m pytest tests/auth/test_session.py -q` |
| 用例数 | 5 |

## 用例清单

1. `test_create_session_is_valid` —— 新建会话后应处于有效状态。
2. `test_create_session_records_role` —— **（新增）** 会话应正确记录用户角色。
3. `test_session_expires_after_ttl` —— 超过 TTL 后会话应失效。
4. `test_logout_invalidates_session` —— 登出后同一 token 应立即失效。
5. `test_unknown_token_is_invalid` —— 未知 token 一律判为无效。
'''


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def _force_utf8_stdout() -> None:
    """规避 Windows PowerShell 控制台 GBK 与 Python UTF-8 输出不匹配导致的中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type:ignore[attr-defined]
        except Exception:
            pass


def _enable_utf8_console() -> None:
    """在 Windows 上尽力把控制台代码页切到 UTF-8(65001)，让交互式运行的中文正常显示。

    chcp 修改的是"控制台"的属性，子进程与父进程共享同一控制台，故对当前 Python 进程同样生效。
    当输出被重定向（无控制台）时该调用无意义，失败即忽略。
    """
    if os.name == "nt":
        try:
            subprocess.run("chcp 65001", shell=True, timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


class _Tee:
    """把 stdout 同时写到控制台与一个 UTF-8 文件，产出一份不受终端编码影响的干净转录。"""

    def __init__(self, orig, fh) -> None:
        self.orig = orig
        self.fh = fh

    def write(self, s: str) -> int:
        self.orig.write(s)
        self.fh.write(s)
        return len(s)

    def flush(self) -> None:
        self.orig.flush()
        self.fh.flush()


def _print_header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def _pi_call(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """模拟 pi（Agent）发起一次记忆工具调用：打印调用轨迹 → dispatch 执行 → 返回结果。"""
    shown = json.dumps(arguments, ensure_ascii=False)
    print(f"\n[pi] 调用工具 {tool_name}({shown})")
    result = dispatch(_STORE, tool_name, arguments)
    print(f"[pi] 工具返回 ok={result.get('ok')} action={result.get('action', '-')}")
    return result


def _reset() -> None:
    """复位到确定性初始状态：重建工作副本、清空记忆库与产物目录。"""
    if WORKSPACE.exists():
        shutil.rmtree(WORKSPACE)
    WORKSPACE.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PRISTINE_PROJECT, WORK_PROJECT)

    if STORE_ROOT.exists():
        shutil.rmtree(STORE_ROOT)
    if ARTIFACTS.exists():
        shutil.rmtree(ARTIFACTS)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)


def _run_pytest(rel_test_path: str) -> dict[str, Any]:
    """在项目根下真正运行 pytest（子进程），返回结构化结果。

    用 sys.executable -B（不写 .pyc，规避"同秒覆写源码后子进程加载陈旧字节码"的陷阱），
    参数以列表传入（不经 shell/shlex，规避 Windows 引号分词陷阱）。
    """
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # -B 不写 .pyc；-o addopts= 清空任何被上层 pytest.ini 注入的额外开关（避免 -q 叠加成 -qq
    # 而吞掉"N passed"汇总行）；-p no:cacheprovider 不落 .pytest_cache，保持工作副本干净。
    cmd = [sys.executable, "-B", "-m", "pytest", rel_test_path, "-q",
           "-o", "addopts=", "-p", "no:cacheprovider"]
    print(f"\n[pi] 运行测试命令（cwd={WORK_PROJECT}）: python -m pytest {rel_test_path} -q")
    proc = subprocess.run(
        cmd,
        cwd=str(WORK_PROJECT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        env=env,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    m = re.search(r"(\d+)\s+passed", out)
    passed_n = int(m.group(1)) if m else 0
    tail = "\n".join([ln for ln in out.splitlines() if ln.strip()][-3:])
    result = {
        "command": f"python -B -m pytest {rel_test_path} -q",
        "returncode": proc.returncode,
        "passed": proc.returncode == 0,
        "passed_count": passed_n,
        "output_tail": tail,
    }
    print(f"[pi] 测试结果: returncode={result['returncode']} "
          f"passed={result['passed']} passed_count={passed_n}")
    return result


def _save_artifact(name: str, payload: dict[str, Any]) -> Path:
    path = ARTIFACTS / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


# 全局记忆库（父进程会话）。步骤 2 会另起子进程构造**全新**的 MemoryStore。
_STORE = MemoryStore(STORE_ROOT)


# --------------------------------------------------------------------------- #
# 步骤 1：把记录真正写入持久化存储（建库结果）
# --------------------------------------------------------------------------- #
def step1_write() -> dict[str, Any]:
    _print_header("步骤 1 · 建库结果：读取会话测试文件与说明，写入项目长期记忆")

    test_abs = WORK_PROJECT / REL_TEST_INITIAL
    doc_abs = WORK_PROJECT / REL_DOC
    print(f"[pi] 读取当前项目的会话测试文件: {test_abs}")
    print(f"[pi] 读取当前项目的测试说明    : {doc_abs}")
    test_text = test_abs.read_text(encoding="utf-8")
    doc_text = doc_abs.read_text(encoding="utf-8")
    n_cases = len(re.findall(r"^def test_", test_text, flags=re.M))
    print(f"[pi] 从测试文件解析出用例数: {n_cases}；说明文件字节数: {len(doc_text)}")

    result = _pi_call(
        "memory_write",
        {
            "project": PROJECT_KEY,
            "title": MEMORY_TITLE,
            "content": {
                "test_path": REL_TEST_INITIAL,
                "run_command": RUN_CMD_INITIAL,
                "doc_path": REL_DOC,
                "case_count": n_cases,
                "summary": "验证用户会话创建/校验/过期/登出的单元测试入口",
            },
            "source": [
                {"path": REL_TEST_INITIAL, "note": "会话测试文件本体"},
                {"path": REL_DOC, "note": "测试说明"},
            ],
            "tags": ["session", "test", "pytest", "auth"],
        },
    )

    # 登记项目根绝对路径，供后续跨会话把相对路径解析成绝对路径
    _STORE.set_project_path(PROJECT_KEY, str(WORK_PROJECT))

    save_path = Path(result["save_path"])
    print("\n---- 记忆已真正落盘（实际保存路径）----")
    print(f"save_path = {save_path}")
    print(f"exists    = {save_path.exists()}  size = {save_path.stat().st_size} bytes")
    print("\n---- 落盘文件内容（memory_store/projects/demo_project.json）----")
    on_disk = json.loads(save_path.read_text(encoding="utf-8"))
    print(json.dumps(on_disk, ensure_ascii=False, indent=2))

    artifact = _save_artifact(
        "step1_write.json",
        {"tool_result": result, "save_path": str(save_path), "on_disk": on_disk},
    )
    print(f"\n[产物] {artifact}")
    return result


# --------------------------------------------------------------------------- #
# 步骤 2：关闭会话 → 全新会话（独立子进程）验证读取与核对
# --------------------------------------------------------------------------- #
def _fresh_read_child() -> None:
    """子进程入口：一个**全新的解释器/全新的会话**，只从磁盘记忆库读取。

    通过环境变量接收 project / query（避免在 Windows 命令行传中文参数的编码问题）。
    以 ensure_ascii=True 打印 JSON，规避管道 GBK 解码问题；父进程再还原中文展示。
    """
    project = os.environ.get("PI_PROJECT", PROJECT_KEY)
    query = os.environ.get("PI_QUERY") or None
    store = MemoryStore(STORE_ROOT)  # 全新实例，仅依赖磁盘
    result = dispatch(store, "memory_find", {"project": project, "query": query})
    # 附带：把记忆里每条 content.test_path 解析成绝对路径并核对是否存在
    checks = []
    for rec in result.get("records", []):
        rel = (rec.get("content") or {}).get("test_path")
        if rel:
            abs_p = store.resolve(project, rel)
            checks.append({"test_path": rel, "abs": str(abs_p), "exists": abs_p.exists()})
    result["existence_checks"] = checks
    print(json.dumps(result, ensure_ascii=True))


def step2_query() -> dict[str, Any]:
    _print_header("步骤 2 · 查询对比与回答：全新会话（独立子进程）读取并核对记忆")

    print("[主会话] 关闭当前会话，启动全新会话（独立子进程，只靠磁盘记忆恢复）……")
    env = dict(os.environ)
    env["PI_PROJECT"] = PROJECT_KEY
    env["PI_QUERY"] = "会话"          # 中文查询经环境变量传入，规避 argv 编码问题
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-B", str(RUN_DEMO), "--_fresh-read"],
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        env=env,
    )
    if proc.returncode != 0:
        print("[全新会话] 子进程失败：")
        print(proc.stdout)
        print(proc.stderr)
        raise SystemExit(1)

    fresh = json.loads(proc.stdout.strip().splitlines()[-1])
    print(f"[全新会话] 子进程 PID 独立，构造全新 MemoryStore；memory_find 命中 "
          f"{fresh.get('count')} 条记忆。")

    print("\n---- 全新会话从长期记忆中查到的『会话测试入口』----")
    for rec in fresh.get("records", []):
        print(json.dumps(rec, ensure_ascii=False, indent=2))

    print("\n---- 查询对比：记忆声称 vs 磁盘实际 ----")
    comparisons = []
    for chk in fresh.get("existence_checks", []):
        row = {
            "memory_test_path": chk["test_path"],
            "resolved_abs": chk["abs"],
            "file_exists": chk["exists"],
            "consistent": chk["exists"],   # 迁移前，记忆与实际应一致
        }
        comparisons.append(row)
        print(f"  记忆: {row['memory_test_path']}  ->  磁盘存在: {row['file_exists']}  "
              f"=> 一致: {row['consistent']}")

    rec0 = fresh["records"][0] if fresh.get("records") else None
    if rec0 and comparisons:
        chk = comparisons[0]
        sources = ", ".join(s["path"] for s in rec0.get("source", []))
        answer = (
            f"从项目 {PROJECT_KEY} 的长期记忆中查到『{rec0['title']}』(revision={rec0['revision']})：\n"
            f"    测试入口 = {rec0['content'].get('test_path')}\n"
            f"    运行命令 = {rec0['content'].get('run_command')}\n"
            f"    来源     = {sources}\n"
            f"    全新会话核对：磁盘上该文件{'存在' if chk['file_exists'] else '不存在'}"
            f"（记忆与实际{'一致' if chk['consistent'] else '不一致'}）。"
        )
    else:
        answer = "未查到会话测试记忆。"
    print("\n---- 全新会话的回答 ----")
    print(answer)

    artifact = _save_artifact(
        "step2_query.json",
        {"fresh_session_find": fresh, "comparison": comparisons, "answer": answer},
    )
    print(f"\n[产物] {artifact}")
    return fresh


# --------------------------------------------------------------------------- #
# 步骤 3：制造项目变化 → 运行新测试命令 → 更新原来的长期记忆
# --------------------------------------------------------------------------- #
def step3_update() -> dict[str, Any]:
    _print_header("步骤 3 · 更新后的回答：迁移会话测试、跑新命令、更新原记忆")

    # 3.1 制造真实的项目变化：把会话测试迁移到 tests/auth/test_session.py
    old_abs = WORK_PROJECT / REL_TEST_INITIAL
    new_abs = WORK_PROJECT / REL_TEST_MIGRATED
    new_abs.parent.mkdir(parents=True, exist_ok=True)
    new_abs.write_text(MIGRATED_TEST_PY, encoding="utf-8")
    (WORK_PROJECT / REL_DOC).write_text(MIGRATED_SESSION_MD, encoding="utf-8")
    if old_abs.exists():
        old_abs.unlink()   # 迁移 = 移动（旧路径不再存在）
    print(f"[项目变化] 迁移 {REL_TEST_INITIAL} -> {REL_TEST_MIGRATED}")
    print(f"[项目变化] 旧路径存在={old_abs.exists()}  新路径存在={new_abs.exists()}")
    print(f"[项目变化] 同步改写测试说明 {REL_DOC}（用例数 4 -> 5）")

    # 3.2 真正运行新的测试命令，拿到实际结果
    test_result = _run_pytest(REL_TEST_MIGRATED)

    # 3.3 依据实际结果，更新原来那条长期记忆（先用 find 定位到它的 id，模拟真实检索）
    found = dispatch(_STORE, "memory_find", {"project": PROJECT_KEY, "query": "会话"})
    rec = found["records"][0]
    old_content = dict(rec["content"])
    print(f"\n[pi] 定位到原记忆 id={rec['id']} revision={rec['revision']}")

    update_result = _pi_call(
        "memory_update",
        {
            "project": PROJECT_KEY,
            "record_id": rec["id"],
            "content": {
                "test_path": REL_TEST_MIGRATED,
                "run_command": RUN_CMD_MIGRATED,
                "case_count": test_result["passed_count"] or 5,
                "last_run": test_result,
                "summary": "会话测试已迁移到 auth 子包并新增角色用例（4 -> 5）",
            },
            "source": [
                {"path": REL_TEST_MIGRATED, "note": "迁移后的会话测试文件"},
                {"path": REL_DOC, "note": "已同步更新的测试说明"},
                {"path": "pytest", "note": f"实跑结果 passed={test_result['passed']}"},
            ],
            "tags": ["session", "test", "pytest", "auth", "migrated"],
        },
    )

    # 3.4 更新后的核对与回答
    new_rec = update_result["record"]
    new_rel = new_rec["content"]["test_path"]
    new_abs_resolved = _STORE.resolve(PROJECT_KEY, new_rel)
    old_abs_resolved = _STORE.resolve(PROJECT_KEY, REL_TEST_INITIAL)
    print("\n---- 更新前 vs 更新后（同一条记忆 id 不变，revision 递增）----")
    diff = {
        "id": new_rec["id"],
        "revision_before": rec["revision"],
        "revision_after": new_rec["revision"],
        "test_path_before": old_content.get("test_path"),
        "test_path_after": new_rec["content"].get("test_path"),
        "run_command_before": old_content.get("run_command"),
        "run_command_after": new_rec["content"].get("run_command"),
        "case_count_before": old_content.get("case_count"),
        "case_count_after": new_rec["content"].get("case_count"),
        "old_path_exists_now": old_abs_resolved.exists(),
        "new_path_exists_now": new_abs_resolved.exists(),
    }
    for k, v in diff.items():
        print(f"  {k}: {v}")

    answer = (
        f"会话测试入口已更新为 {new_rec['content']['test_path']}，"
        f"运行命令 {new_rec['content']['run_command']}，"
        f"实跑 {'通过' if test_result['passed'] else '失败'}"
        f"（{test_result['passed_count']} passed）。"
        f"旧路径已不存在（{diff['old_path_exists_now']}），新路径存在（{diff['new_path_exists_now']}）；"
        f"记忆 revision {diff['revision_before']} -> {diff['revision_after']}。"
    )
    print("\n---- 更新后的回答 ----")
    print(answer)

    save_path = Path(update_result["save_path"])
    print(f"\n[落盘] 更新后的记忆文件: {save_path}")
    print(json.dumps(json.loads(save_path.read_text(encoding="utf-8")),
                     ensure_ascii=False, indent=2))

    artifact = _save_artifact(
        "step3_update.json",
        {
            "project_change": {
                "migrated_from": REL_TEST_INITIAL,
                "migrated_to": REL_TEST_MIGRATED,
                "old_exists": old_abs_resolved.exists(),
                "new_exists": new_abs_resolved.exists(),
            },
            "pytest_result": test_result,
            "update_tool_result": update_result,
            "diff": diff,
            "answer": answer,
        },
    )
    print(f"\n[产物] {artifact}")
    return update_result


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main(argv: list[str]) -> int:
    _force_utf8_stdout()

    # 隐藏模式：全新会话子进程入口（只读磁盘记忆并打印 JSON）——必须在复位前返回
    if "--_fresh-read" in argv:
        _fresh_read_child()
        return 0

    _enable_utf8_console()
    _reset()

    # 复位后再挂 tee：把整场演示同步写成一份干净 UTF-8 转录，便于截图/复查
    transcript = ARTIFACTS / "console_transcript.txt"
    tee = _Tee(sys.stdout, transcript.open("w", encoding="utf-8"))
    sys.stdout = tee
    try:
        print("可用记忆工具（pi 可调用）：",
              [t["function"]["name"] for t in MEMORY_TOOLS])
        print(f"[复位] 工作副本: {WORK_PROJECT}")
        print(f"[复位] 记忆库根: {STORE_ROOT}")

        step1_write()
        step2_query()
        step3_update()

        _print_header("演示完成 · 三张截图对应位置")
        print("  截图 1（建库结果）    : 步骤 1 输出的 save_path 与落盘 JSON；"
              "或 artifacts/step1_write.json")
        print("  截图 2（查询对比与回答）: 步骤 2 的『查询对比』与『全新会话的回答』；"
              "或 artifacts/step2_query.json")
        print("  截图 3（更新后的回答） : 步骤 3 的『更新前 vs 更新后』与『更新后的回答』；"
              "或 artifacts/step3_update.json")
        print(f"\n记忆库最终状态目录: {STORE_ROOT / 'projects'}")
        print(f"干净 UTF-8 转录    : {transcript}")
    finally:
        sys.stdout = tee.orig
        tee.fh.close()
    return 0



if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
