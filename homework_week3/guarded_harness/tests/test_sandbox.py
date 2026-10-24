"""tests/test_sandbox.py —— 执行边界（S1 路径逃逸 / S2 软链接·敏感目录 / S3 命令白名单 / S4 超时终止 / 输出·环境边界）。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from conftest import make_sandbox
from sandbox import SandboxViolation, make_default_sandbox


def _link_dir(link: Path, target: Path) -> bool:
    """尽力创建一个目录链接（软链接优先，Windows 回退到 junction）。失败返回 False。"""

    try:
        link.symlink_to(target, target_is_directory=True)
        return True
    except (OSError, NotImplementedError, AttributeError):
        pass
    if os.name == "nt":
        try:
            import _winapi  # type: ignore

            _winapi.CreateJunction(str(target), str(link))
            return True
        except Exception:  # noqa: BLE001
            try:
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                    check=True, capture_output=True,
                )
                return True
            except Exception:  # noqa: BLE001
                return False
    return False


# --------------------------------------------------------------------------- #
# S1：路径逃逸
# --------------------------------------------------------------------------- #
def test_resolve_path_inside_workspace_ok(sandbox, workspace):
    (workspace / "a.txt").write_text("hello", encoding="utf-8")
    resolved = sandbox.resolve_path("a.txt", must_exist=True)
    assert resolved.rel == "a.txt"
    assert resolved.real.exists()


def test_resolve_path_relative_escape_denied(sandbox):
    # S1：`../` 相对逃逸必须被拒。
    with pytest.raises(SandboxViolation) as ei:
        sandbox.resolve_path("../secret.txt")
    assert ei.value.code == "PATH_ESCAPE"


def test_resolve_path_deep_relative_escape_denied(sandbox):
    with pytest.raises(SandboxViolation) as ei:
        sandbox.resolve_path("artifacts/../../.env")
    assert ei.value.code in {"PATH_ESCAPE", "SENSITIVE_PATH"}


def test_resolve_path_absolute_escape_denied(sandbox, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(SandboxViolation) as ei:
        sandbox.resolve_path(str(outside))
    assert ei.value.code == "PATH_ESCAPE"


# --------------------------------------------------------------------------- #
# S2：敏感目录 / 软链接逃逸
# --------------------------------------------------------------------------- #
def test_sensitive_env_denied(sandbox, workspace):
    (workspace / ".env").write_text("SECRET=1", encoding="utf-8")
    with pytest.raises(SandboxViolation) as ei:
        sandbox.resolve_path(".env")
    assert ei.value.code == "SENSITIVE_PATH"


def test_sensitive_git_subpath_denied(sandbox, workspace):
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("[core]", encoding="utf-8")
    with pytest.raises(SandboxViolation) as ei:
        sandbox.resolve_path(".git/config")
    assert ei.value.code == "SENSITIVE_PATH"


def test_symlink_escape_denied(sandbox, workspace, tmp_path):
    # S2：软链接/junction 指向工作区外，按“真实落点”判定为逃逸。
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (outside / "secret.txt").write_text("top-secret", encoding="utf-8")
    link = workspace / "escape"
    if not _link_dir(link, outside):
        pytest.skip("当前平台/权限不支持创建软链接或 junction")
    with pytest.raises(SandboxViolation) as ei:
        sandbox.resolve_path("escape/secret.txt", must_exist=True)
    assert ei.value.code == "PATH_ESCAPE"


# --------------------------------------------------------------------------- #
# S3：命令白名单
# --------------------------------------------------------------------------- #
def test_command_allowed_returns_tokens(sandbox):
    tokens = sandbox.assert_command_allowed("python -m pytest -q")
    assert tokens[:3] == ["python", "-m", "pytest"]


def test_command_not_allowed(sandbox):
    with pytest.raises(SandboxViolation) as ei:
        sandbox.assert_command_allowed("curl http://evil.example.com")
    assert ei.value.code == "COMMAND_NOT_ALLOWED"


def test_dangerous_command_denied_even_if_prefixed(sandbox):
    # 危险模式优先于白名单前缀。
    with pytest.raises(SandboxViolation) as ei:
        sandbox.assert_command_allowed("python -c \"import os; os.system('rm -rf /')\"")
    assert ei.value.code == "DANGEROUS_COMMAND"


def test_empty_command_denied(sandbox):
    with pytest.raises(SandboxViolation) as ei:
        sandbox.assert_command_allowed("   ")
    assert ei.value.code == "COMMAND_INVALID"


# --------------------------------------------------------------------------- #
# 文件副作用
# --------------------------------------------------------------------------- #
def test_read_write_list_roundtrip(sandbox, workspace):
    sandbox.write_text("sub/dir/out.txt", "content")
    text, resolved = sandbox.read_text("sub/dir/out.txt")
    assert text == "content"
    assert resolved.rel.replace("\\", "/") == "sub/dir/out.txt"
    entries, _ = sandbox.list_dir("sub/dir")
    assert entries == ["out.txt"]


def test_read_missing_file_denied(sandbox):
    with pytest.raises(SandboxViolation) as ei:
        sandbox.read_text("nope.txt")
    assert ei.value.code == "PATH_NOT_FOUND"


def test_list_dir_on_file_denied(sandbox, workspace):
    (workspace / "f.txt").write_text("x", encoding="utf-8")
    with pytest.raises(SandboxViolation) as ei:
        sandbox.list_dir("f.txt")
    assert ei.value.code == "PATH_INVALID"


# --------------------------------------------------------------------------- #
# S4：超时真实终止
# --------------------------------------------------------------------------- #
def test_run_command_timeout_terminates(sandbox):
    result = sandbox.run_command("python -c \"import time; time.sleep(30)\"", timeout_seconds=1.0)
    assert result.timed_out is True
    assert result.terminated is True
    # 真实终止：不应等满 30s（留足冗余）。
    assert result.elapsed_ms < 15000


def test_run_command_captures_output(sandbox):
    result = sandbox.run_command("python -c \"print('hello-out')\"")
    assert result.returncode == 0
    assert "hello-out" in result.stdout
    assert result.timed_out is False


# --------------------------------------------------------------------------- #
# 输出 / 环境边界
# --------------------------------------------------------------------------- #
def test_output_truncation(workspace):
    sb = make_sandbox(workspace, max_output_chars=100)
    result = sb.run_command("python -c \"print('x' * 5000)\"")
    assert result.truncated is True
    assert "truncated" in result.stdout
    assert len(result.stdout) < 300


def test_output_redaction(sandbox):
    result = sandbox.run_command("python -c \"print('api_key=abc123secret')\"")
    assert "abc123secret" not in result.stdout
    assert "REDACTED" in result.stdout


def test_env_allowlist_excludes_secrets(sandbox, monkeypatch):
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "leak-me")
    result = sandbox.run_command("python -c \"import os; print(os.environ.get('SUPER_SECRET_TOKEN', 'MISSING'))\"")
    assert "MISSING" in result.stdout
    assert "leak-me" not in result.stdout


# --------------------------------------------------------------------------- #
# 工作区指纹
# --------------------------------------------------------------------------- #
def test_workspace_fingerprint_changes_on_edit(sandbox, workspace):
    (workspace / "m.py").write_text("a = 1\n", encoding="utf-8")
    fp1 = sandbox.workspace_fingerprint()
    (workspace / "m.py").write_text("a = 2\n", encoding="utf-8")
    fp2 = sandbox.workspace_fingerprint()
    assert fp1 != fp2


def test_workspace_fingerprint_ignores_pycache_and_hidden(sandbox, workspace):
    (workspace / "keep.py").write_text("a = 1\n", encoding="utf-8")
    fp1 = sandbox.workspace_fingerprint()
    cache = workspace / "__pycache__"
    cache.mkdir()
    (cache / "keep.cpython-311.pyc").write_bytes(b"\x00\x01")
    hidden = workspace / ".secret"
    hidden.mkdir()
    (hidden / "x.py").write_text("b = 1\n", encoding="utf-8")
    fp2 = sandbox.workspace_fingerprint()
    assert fp1 == fp2


def test_make_default_sandbox_type(workspace):
    sb = make_default_sandbox(workspace)
    assert sb.root == workspace.resolve()
