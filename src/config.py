"""全局配置模块：从项目根目录 .env 文件加载配置项。

优先级：系统环境变量 > .env 文件 > Settings 中的默认值。
"""
import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

# 项目根目录（本文件位于 src/ 下，向上一级）
BASE_DIR = Path(__file__).resolve().parents[1]

# 加载 .env 文件，不覆盖已存在的系统环境变量
load_dotenv(BASE_DIR / ".env")


class Settings(BaseModel):
    """全局配置项，字段名对应 .env 中的环境变量名（大写）。"""
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    # DeepSeek 官方模型名（适配器默认档位，注册表可按档位覆盖）
    deepseek_model_name: str = "deepseek-v4-flash"
    # llama.cpp 本地部署配置：OpenAI 兼容接口，无需 API Key
    llama_base_url: str = "http://localhost:8080/v1"
    llama_model_name: str = "Qwen3.8-27B-NVFP4-MTP-HIGHEST"
    # Qoder Agent SDK 配置：认证默认用 qodercli 本机登录态，token 非必填
    # 注：qoderAdapter 已限定纯问答模式（max_turns=1），无需配置轮数
    qoder_access_token: str = ""
    qoder_model: str = ""
    qoder_permission_mode: str = ""
    # 网关韧性配置：按模型独立限流（令牌桶），默认值偏低便于演示 429，生产可调高
    rate_limit_rps: float = 2.0
    rate_limit_burst: int = 2
    # 可观测性：SQLite 落盘路径（相对项目根目录）
    observability_db: str = "logs/observability.db"
    # 提示词模板库文件目录（相对项目根目录）
    prompts_dir: str = "prompts"

@lru_cache
def get_settings() -> Settings:
    """获取全局配置单例。"""
    values = {
        # 环境变量为空字符串时视为未设置，回退默认值（避免 int 等类型转换失败）
        name: os.getenv(name.upper()) or field.default
        for name, field in Settings.model_fields.items()
    }
    return Settings(**values)
