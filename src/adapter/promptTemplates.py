"""受控系统提示词兼容层：从版本化模板仓库解析模板名（取 latest 版本）。

历史上模板以静态字典登记于本文件；现已迁移至 prompts/ 目录 +
src/prompts/ 版本化管理。本模块保留 get_system_prompt / list_templates
接口，供 ModelRequest 与既有测试按"模板名"引用（自动取 latest 版本）。

网关 HTTP 入口支持更完整的引用语法（name@v1、变量替换），见 src/prompts/registry.py。
防注入约定不变：系统提示词一律来自模板库登记内容，禁止自由文本。
"""
from src.prompts.repository import get_repository


def get_system_prompt(name: str) -> str:
    """按模板名解析系统提示词（latest 版本）；未登记模板直接报错。"""
    repo = get_repository()
    version = repo.latest_version(name)  # 未登记时抛 GatewayError(TEMPLATE_NOT_FOUND)
    return repo.get(name, version).content


def list_templates() -> list[str]:
    """列出全部已登记的模板名。"""
    return [info.name for info in get_repository().list()]
