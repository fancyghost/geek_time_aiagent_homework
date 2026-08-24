"""模板存储抽象：TemplateRepository 接口 + 文件实现（FileTemplateRepository）。

存储可替换：未来切换 DB 只需新增 TemplateRepository 实现并替换
get_repository 的装配点，registry/适配层与网关均不感知存储细节。

文件布局约定：
    prompts/<name>/v<N>.md     模板正文（含 {{var}} 占位符）
    prompts/manifest.json      登记表：latest 版本 / 描述 / 必填变量清单
"""
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from src.config import get_settings
from src.errors import ErrorCode, GatewayError


@dataclass(frozen=True)
class TemplateContent:
    """单个版本的模板内容。"""
    name: str
    version: int
    content: str
    variables: list[str] = field(default_factory=list)  # manifest 登记的必填变量


@dataclass(frozen=True)
class TemplateInfo:
    """模板元信息（用于列表展示）。"""
    name: str
    latest_version: int
    versions: list[int]
    description: str
    variables: list[str]


class TemplateRepository(ABC):
    """模板存储抽象接口：文件 / DB 等实现均遵循此契约。"""

    @abstractmethod
    def get(self, name: str, version: int) -> TemplateContent:
        """读取指定模板的指定版本；不存在时抛 GatewayError(TEMPLATE_NOT_FOUND)。"""
        raise NotImplementedError

    @abstractmethod
    def latest_version(self, name: str) -> int:
        """返回模板的 latest 版本号；模板不存在时抛 GatewayError(TEMPLATE_NOT_FOUND)。"""
        raise NotImplementedError

    @abstractmethod
    def list(self) -> list[TemplateInfo]:
        """列出全部已登记模板的元信息。"""
        raise NotImplementedError


class FileTemplateRepository(TemplateRepository):
    """文件系统实现：目录 + manifest.json 登记。"""

    def __init__(self, root: Path | str | None = None) -> None:
        settings = get_settings()
        base = Path(root if root is not None else settings.prompts_dir)
        self.root = base if base.is_absolute() else (
            Path(__file__).resolve().parents[2] / base
        )

    def _manifest(self) -> dict:
        manifest_path = self.root / "manifest.json"
        if not manifest_path.exists():
            raise GatewayError(
                ErrorCode.TEMPLATE_NOT_FOUND,
                "模板库未初始化",
                f"缺少 {manifest_path}",
            )
        return json.loads(manifest_path.read_text(encoding="utf-8"))

    def latest_version(self, name: str) -> int:
        entry = self._manifest().get(name)
        if entry is None:
            raise GatewayError(
                ErrorCode.TEMPLATE_NOT_FOUND,
                f"模板 {name!r} 未在模板库登记",
                f"已登记模板：{sorted(self._manifest())}",
            )
        return int(entry["latest"])

    def get(self, name: str, version: int) -> TemplateContent:
        entry = self._manifest().get(name)
        if entry is None:
            raise GatewayError(
                ErrorCode.TEMPLATE_NOT_FOUND,
                f"模板 {name!r} 未在模板库登记",
                f"已登记模板：{sorted(self._manifest())}",
            )
        path = self.root / name / f"v{version}.md"
        if not path.exists():
            raise GatewayError(
                ErrorCode.TEMPLATE_NOT_FOUND,
                f"模板 {name!r} 不存在版本 v{version}",
                f"latest 为 v{entry['latest']}",
            )
        return TemplateContent(
            name=name,
            version=version,
            content=path.read_text(encoding="utf-8"),
            variables=list(entry.get("variables", [])),
        )

    def list(self) -> list[TemplateInfo]:
        manifest = self._manifest()
        infos: list[TemplateInfo] = []
        for name, entry in sorted(manifest.items()):
            versions = sorted(
                int(p.stem[1:])
                for p in (self.root / name).glob("v*.md")
                if p.stem[1:].isdigit()
            )
            infos.append(
                TemplateInfo(
                    name=name,
                    latest_version=int(entry["latest"]),
                    versions=versions,
                    description=entry.get("description", ""),
                    variables=list(entry.get("variables", [])),
                )
            )
        return infos


_repository: TemplateRepository | None = None


def get_repository() -> TemplateRepository:
    """全局模板仓库（懒加载单例）；切换 DB 实现时仅需替换此处装配。"""
    global _repository
    if _repository is None:
        _repository = FileTemplateRepository()
    return _repository
