"""sandbox.py —— 执行边界。

职责（对应交付物 sandbox.py 与验收 S1/S2/S3/S4）：
- 文件路径与敏感目录边界：解析真实落点（含软链接），越界或命中敏感目录即拒绝。
  - S1：``artifacts/../../.env`` 这类相对路径逃逸被拒绝。
  - S2：软链接逃逸 / 敏感目录访问，按“真实落点”校验后拒绝。
- 命令白名单：``run_test`` 只允许配置内命令，配置外命令不执行（S3）。
- 超时与取消：真实终止进程组，而不是只停止等待（S4）。
- 环境变量边界：只透传白名单变量，避免密钥泄漏给子进程。
- 输出边界：截断超长输出，脱敏命中的敏感串。

本模块是纯 Python、无 smolagents 依赖，可独立单测。
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence


class SandboxViolation(RuntimeError):
    """越界异常：携带稳定的 ``code``，供 Gate 转成结构化拒绝。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ResolvedPath:
    """一次成功的路径解析结果。``real`` 为解析软链接后的真实落点。"""

    requested: str
    absolute: Path
    real: Path
    rel: str


@dataclass(frozen=True, slots=True)
class CommandResult:
    """命令执行结果（受超时与输出边界约束）。"""

    command: str
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    terminated: bool
    elapsed_ms: int
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    """沙箱策略：白名单、敏感目录、超时、输出与环境边界。"""

    workspace_root: Path
    # 命令白名单：命令必须以下列某个前缀开头（按 token 序列比较）
    allowed_command_prefixes: tuple[tuple[str, ...], ...] = (
        ("pytest",),
        ("python", "-m", "pytest"),
        ("python", "-m", "unittest"),
    )
    # 敏感目录/文件：真实落点命中即拒绝（相对 workspace 或绝对）
    sensitive_names: tuple[str, ...] = (".env", ".git", "id_rsa", ".ssh", "secrets", ".aws", "credentials")
    # 危险命令正则：即便前缀命中白名单也拒绝
    dangerous_patterns: tuple[re.Pattern[str], ...] = (
        re.compile(r"\brm\s+-rf\b", re.I),
        re.compile(r"\bgit\s+push\s+--force\b", re.I),
        re.compile(r"\bgit\s+reset\s+--hard\b", re.I),
        re.compile(r"\bsudo\b", re.I),
        re.compile(r"\bmkfs\b", re.I),
        re.compile(r"\bshutdown\b", re.I),
        re.compile(r">\s*/dev/", re.I),
    )
    default_timeout_seconds: float = 5.0
    max_output_chars: int = 8000
    # 只透传这些环境变量给子进程（其余一律剔除）
    env_allowlist: tuple[str, ...] = ("PATH", "PYTHONPATH", "PYTHONUTF8", "SYSTEMROOT", "PATHEXT", "HOME", "LANG", "LC_ALL")
    # 输出脱敏：命中即替换
    redact_patterns: tuple[re.Pattern[str], ...] = (
        re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[=:]\s*\S+"),
    )
    extra_sensitive_globs: tuple[str, ...] = ()


class Sandbox:
    """执行边界守卫：路径解析、命令白名单、超时终止、输出与环境边界。"""

    def __init__(self, policy: SandboxPolicy) -> None:
        self.policy = policy
        self.root = policy.workspace_root.resolve()

    # ------------------------------------------------------------------ #
    # 路径边界（S1 / S2）
    # ------------------------------------------------------------------ #
    def resolve_path(self, raw: str, *, for_write: bool = False, must_exist: bool = False) -> ResolvedPath:
        """把模型给出的路径解析为受控的真实落点，越界即抛 ``SandboxViolation``。

        步骤：
        1. 相对路径以 workspace_root 为基准拼接；
        2. ``os.path.realpath`` 解析软链接得到真实落点；
        3. 真实落点必须仍在 workspace_root（真实根）之内，否则判为逃逸；
        4. 命中敏感目录/文件则拒绝。
        """

        if not isinstance(raw, str) or raw == "":
            raise SandboxViolation("PATH_INVALID", "路径必须是非空字符串")

        candidate = Path(raw)
        absolute = candidate if candidate.is_absolute() else (self.root / candidate)
        # 规范化（不解析软链接）用于展示；realpath 解析软链接用于安全判定。
        absolute_norm = Path(os.path.normpath(str(absolute)))
        real = Path(os.path.realpath(str(absolute_norm)))
        real_root = self.root

        # S1/S2：真实落点必须位于真实根之内。软链接指向外部会在此暴露。
        try:
            rel = real.relative_to(real_root)
        except ValueError:
            raise SandboxViolation(
                "PATH_ESCAPE",
                f"路径逃逸出工作区：requested={raw!r} real={str(real)!r} root={str(real_root)!r}",
            )

        # S2：命中敏感目录/文件（按真实落点的相对路径与各级名字判定）。
        rel_posix = rel.as_posix()
        parts = rel.parts
        for name in self.policy.sensitive_names:
            if name in parts or rel_posix == name or rel_posix.startswith(name + "/"):
                raise SandboxViolation("SENSITIVE_PATH", f"命中敏感路径：{rel_posix!r}")
        for glob_pat in self.policy.extra_sensitive_globs:
            if rel.match(glob_pat):
                raise SandboxViolation("SENSITIVE_PATH", f"命中敏感模式：{glob_pat!r}")

        # 目标须存在（读取/列目录）；写入允许不存在（稍后创建父目录）。
        if must_exist and not real.exists():
            raise SandboxViolation("PATH_NOT_FOUND", f"路径不存在：{rel_posix!r}")
        if must_exist and not for_write and real.is_dir() is False and not real.is_file():
            raise SandboxViolation("PATH_INVALID", f"不是常规文件：{rel_posix!r}")
        if for_write and real.exists() and not real.is_file():
            raise SandboxViolation("PATH_INVALID", f"写入目标不是文件：{rel_posix!r}")

        return ResolvedPath(requested=raw, absolute=absolute_norm, real=real, rel=rel_posix)

    # ------------------------------------------------------------------ #
    # 命令白名单（S3）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _tokenize(command: str) -> list[str]:
        """跨平台命令分词。

        Windows 上 ``shlex.split(posix=False)`` 会**保留包裹引号**，导致
        ``python -c "code"`` 的 ``-c`` 实参变成带引号的字符串字面量（被当作空表达式求值），
        因此分词后再去除每个 token 成对的首尾引号；POSIX 下 ``posix=True`` 已正确去引号。
        """

        posix = os.name != "nt"
        tokens = shlex.split(command, posix=posix)
        if posix:
            return tokens
        cleaned: list[str] = []
        for tok in tokens:
            if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ('"', "'"):
                tok = tok[1:-1]
            cleaned.append(tok)
        return cleaned

    def assert_command_allowed(self, command: str) -> list[str]:
        """校验命令是否在白名单内且不含危险模式；通过则返回分词结果。"""

        if not isinstance(command, str) or not command.strip():
            raise SandboxViolation("COMMAND_INVALID", "命令必须是非空字符串")

        # 危险命令：即便前缀命中白名单也直接拒绝。
        for pattern in self.policy.dangerous_patterns:
            if pattern.search(command):
                raise SandboxViolation("DANGEROUS_COMMAND", f"命中危险命令模式：{pattern.pattern}")

        tokens = self._tokenize(command)
        if not tokens:
            raise SandboxViolation("COMMAND_INVALID", f"命令解析为空：{command!r}")

        for prefix in self.policy.allowed_command_prefixes:
            if len(tokens) >= len(prefix) and tuple(tokens[: len(prefix)]) == tuple(prefix):
                return tokens

        raise SandboxViolation("COMMAND_NOT_ALLOWED", f"命令不在白名单内：{command!r}")

    # ------------------------------------------------------------------ #
    # 文件副作用（受路径边界约束）
    # ------------------------------------------------------------------ #
    def read_text(self, path: str) -> tuple[str, ResolvedPath]:
        resolved = self.resolve_path(path, must_exist=True)
        text = resolved.real.read_text(encoding="utf-8")
        return text, resolved

    def write_text(self, path: str, content: str) -> ResolvedPath:
        resolved = self.resolve_path(path, for_write=True)
        resolved.real.parent.mkdir(parents=True, exist_ok=True)
        resolved.real.write_text(content, encoding="utf-8")
        return resolved

    def list_dir(self, path: str) -> tuple[list[str], ResolvedPath]:
        resolved = self.resolve_path(path, must_exist=True)
        if not resolved.real.is_dir():
            raise SandboxViolation("PATH_INVALID", f"不是目录：{resolved.rel!r}")
        entries = sorted(p.name for p in resolved.real.iterdir())
        return entries, resolved

    def sha256_of(self, path: str) -> str:
        resolved = self.resolve_path(path, must_exist=True)
        return hashlib.sha256(resolved.real.read_bytes()).hexdigest()

    # ------------------------------------------------------------------ #
    # 命令执行（S4：超时真实终止进程组；环境/输出边界）
    # ------------------------------------------------------------------ #
    def run_command(self, command: str, *, timeout_seconds: float | None = None) -> CommandResult:
        """在白名单与超时约束内执行命令，超时则真实终止整个进程组。"""

        tokens = self.assert_command_allowed(command)
        timeout = self.policy.default_timeout_seconds if timeout_seconds is None else timeout_seconds
        env = self._build_env()
        started = time.perf_counter()

        popen_kwargs: dict[str, object] = {
            "cwd": str(self.root),
            "env": env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if os.name == "nt":
            # 新建进程组，便于整组终止。
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            popen_kwargs["start_new_session"] = True

        proc = subprocess.Popen(tokens, **popen_kwargs)  # type: ignore[arg-type]
        timed_out = False
        terminated = False
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            terminated = self._terminate_process_group(proc)
            # 终止后再回收一次，确保拿到已产生的部分输出。
            try:
                stdout, stderr = proc.communicate(timeout=2)
            except Exception:  # noqa: BLE001 - 回收失败不影响“已终止”的结论
                stdout, stderr = "", ""
        elapsed_ms = round((time.perf_counter() - started) * 1000)

        stdout, out_truncated = self._bound_output(stdout or "")
        stderr, err_truncated = self._bound_output(stderr or "")
        return CommandResult(
            command=command,
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            terminated=terminated or (proc.returncode is not None and proc.returncode < 0),
            elapsed_ms=elapsed_ms,
            truncated=out_truncated or err_truncated,
        )

    def _terminate_process_group(self, proc: subprocess.Popen) -> bool:
        """真实终止进程组。返回是否确认进程已结束。"""

        try:
            if os.name == "nt":
                # /T 终止进程树，/F 强制。
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    check=False,
                )
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:  # noqa: BLE001 - 兜底再杀一次主进程
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            pass
        return proc.poll() is not None

    # ------------------------------------------------------------------ #
    # 环境 / 输出边界
    # ------------------------------------------------------------------ #
    def _build_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        for key in self.policy.env_allowlist:
            if key in os.environ:
                env[key] = os.environ[key]
        # 强制 UTF-8，避免子进程中文输出乱码。
        env.setdefault("PYTHONUTF8", "1")
        return env

    def _truncate(self, text: str) -> str:
        limit = self.policy.max_output_chars
        if len(text) > limit:
            return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"
        return text

    def _redact(self, text: str) -> str:
        for pattern in self.policy.redact_patterns:
            text = pattern.sub(lambda m: f"{m.group(1)}=***REDACTED***", text)
        return text

    def _bound_output(self, text: str) -> tuple[str, bool]:
        truncated = len(text) > self.policy.max_output_chars
        return self._redact(self._truncate(text)), truncated

    # ------------------------------------------------------------------ #
    # 工作区指纹（供 H2 / C4 判定证据与审批是否失效）
    # ------------------------------------------------------------------ #
    def workspace_fingerprint(self, *, extensions: Sequence[str] = (".py", ".txt", ".md", ".cfg", ".toml", ".ini", ".json")) -> str:
        """对工作区内受控文件计算稳定指纹。

        任何被跟踪文件的内容变化都会改变指纹，从而让“旧的测试证据/审批”失效。
        跳过隐藏目录（.git 等）与 __pycache__，避免噪声。
        """

        hasher = hashlib.sha256()
        entries: list[str] = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.root).as_posix()
            if any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(self.root).parts):
                continue
            if path.suffix.lower() not in extensions:
                continue
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                digest = "unreadable"
            entries.append(f"{rel}:{digest}")
        hasher.update("\n".join(entries).encode("utf-8"))
        return hasher.hexdigest()


def make_default_sandbox(workspace_root: str | Path, **policy_overrides: object) -> Sandbox:
    """便捷构造：以给定 workspace 根目录创建默认策略沙箱。"""

    policy = SandboxPolicy(workspace_root=Path(workspace_root), **policy_overrides)  # type: ignore[arg-type]
    return Sandbox(policy)
