"""模板渲染入口：版本引用解析 + {{var}} 变量替换。

引用语法：
    "db_assistant"        → latest 版本
    "db_assistant@latest" → latest 版本
    "db_assistant@v1"     → 指定 v1

防注入约定：系统提示词只允许来自模板库登记内容；variables 仅用于填充
模板预先声明的 {{var}} 占位符，不会引入模板之外的指令文本。
"""
import re

from src.errors import ErrorCode, GatewayError
from src.prompts.repository import TemplateContent, TemplateRepository, get_repository

# 模板引用语法：名称[@v版本号|@latest]
_REF_PATTERN = re.compile(r"^(?P<name>[A-Za-z0-9_\-]+)(?:@(?P<ver>latest|v\d+))?$")


def parse_ref(ref: str) -> tuple[str, str]:
    """解析模板引用，返回 (模板名, 版本标识)；非法引用抛 INVALID_REQUEST。"""
    match = _REF_PATTERN.match(ref.strip())
    if not match:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST,
            f"非法模板引用：{ref!r}",
            "格式应为 name 或 name@v1 / name@latest",
        )
    return match.group("name"), match.group("ver") or "latest"


def render_template(
    ref: str,
    variables: dict[str, str] | None = None,
    repo: TemplateRepository | None = None,
) -> TemplateContent:
    """按引用取模板并完成变量替换，返回内容已渲染的 TemplateContent。

    校验规则：manifest 登记的必填变量缺失 → TEMPLATE_VAR_MISSING(400)；
    传入未声明的多余变量 → INVALID_REQUEST(400)。
    """
    repository = repo or get_repository()
    name, ver = parse_ref(ref)
    version = repository.latest_version(name) if ver == "latest" else int(ver[1:])
    template = repository.get(name, version)

    provided = dict(variables or {})

    # 必填变量缺失检查
    missing = [var for var in template.variables if var not in provided]
    if missing:
        raise GatewayError(
            ErrorCode.TEMPLATE_VAR_MISSING,
            f"模板 {name!r} 缺少必填变量：{missing}",
            f"需要变量：{template.variables}",
        )
    # 未声明变量检查（防止调用方塞入模板外的占位符）
    unknown = [var for var in provided if var not in template.variables]
    if unknown:
        raise GatewayError(
            ErrorCode.INVALID_REQUEST,
            f"模板 {name!r} 未声明变量：{unknown}",
            f"模板声明的变量：{template.variables}",
        )

    content = template.content
    for key, value in provided.items():
        content = content.replace("{{" + key + "}}", str(value))
    # 内容副本，版本号保持原值（渲染不改变版本语义）
    return TemplateContent(
        name=template.name,
        version=template.version,
        content=content,
        variables=template.variables,
    )
