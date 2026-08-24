"""提示词模板版本管理：模板存储、变量替换与版本引用。"""
from src.prompts.registry import render_template
from src.prompts.repository import (
    FileTemplateRepository,
    TemplateContent,
    TemplateInfo,
    TemplateRepository,
    get_repository,
)

__all__ = [
    "FileTemplateRepository",
    "TemplateContent",
    "TemplateInfo",
    "TemplateRepository",
    "get_repository",
    "render_template",
]
