"""登录资料副本管理。

与第一题 demo_project → .demo_workspace 的思路一致：
  * login_docs/ 是仓库里提交的**只读初始资料**，演示绝不改它；
  * 每次运行先整目录拷贝到 .rag_workspace/login_docs/，再在副本上做"更新验证"；
  * 因此演示可反复运行、结果一致，且随时能查看原始资料。

第 3 步的"更新"= 在副本里把登录会话有效期从 30 分钟精确改写为 20 分钟，
随后重建两路索引，使旧的 30 分钟分块**退出当前检索**。
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import config


@dataclass
class UpdateReport:
    """一次有效期改写的结果，便于演示打印/落盘。"""

    from_phrase: str
    to_phrase: str
    changed_files: list[str] = field(default_factory=list)
    replacements: int = 0

    def to_dict(self) -> dict:
        return {
            "from_phrase": self.from_phrase,
            "to_phrase": self.to_phrase,
            "changed_files": self.changed_files,
            "replacements": self.replacements,
        }


def reset_workspace() -> Path:
    """复位工作区：删除旧副本，从只读初始资料整目录拷贝，返回副本目录。"""
    if config.WORKSPACE.exists():
        shutil.rmtree(config.WORKSPACE)
    config.WORKSPACE.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(config.PRISTINE_DOCS, config.WORK_DOCS)
    return config.WORK_DOCS


def list_docs(docs_dir: Path | None = None) -> list[Path]:
    """列出资料目录下的全部 .md 文件（按文件名排序，保证确定性）。"""
    d = docs_dir or config.WORK_DOCS
    return sorted(d.glob("*.md"))


def read_docs(docs_dir: Path | None = None) -> dict[str, str]:
    """读取全部资料为 {文件名: 文本}（UTF-8）。"""
    return {p.name: p.read_text(encoding="utf-8") for p in list_docs(docs_dir)}


def apply_validity_update(
    minutes: int = config.UPDATED_VALIDITY_MINUTES,
    docs_dir: Path | None = None,
) -> UpdateReport:
    """在资料副本里把有效期从原始分钟数改写为 minutes（精确、无歧义的整词替换）。

    只替换 config 里定义的"有效期短语"（如 "30 分钟" -> "20 分钟"）。
    资料中其它带"分钟"的表述（如"锁定 15 分钟"）不含该确切短语，故不受影响。
    """
    d = docs_dir or config.WORK_DOCS
    from_phrase = config.original_validity_phrase()
    to_phrase = config.VALIDITY_PHRASE_TEMPLATE.format(minutes=minutes)
    report = UpdateReport(from_phrase=from_phrase, to_phrase=to_phrase)

    for path in list_docs(d):
        text = path.read_text(encoding="utf-8")
        n = text.count(from_phrase)
        if n == 0:
            continue
        new_text = text.replace(from_phrase, to_phrase)
        path.write_text(new_text, encoding="utf-8")
        report.changed_files.append(path.name)
        report.replacements += n
    return report
