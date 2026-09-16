from pathlib import Path

from pydantic import AliasChoices, BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from config import PostgreConfig

BASE_DIR = Path(__file__).resolve().parent.parent


class AgentPostgreConfig(PostgreConfig):
    """单个 PostgreSQL 库的连接参数。"""

    host: str = "127.0.0.1"
    port: int = 5432
    user: str = "postgres"
    password: str = "1234"
    db_name: str = "agents"


class ModelConfig(BaseSettings):
    """LLM 模式配置，敏感字段从指定环境变量读取。

    优先级：系统环境变量 > 项目根目录 .env > 字段默认值。
    """

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    model: str = Field("", validation_alias="OPEN_MODEL")
    base_url: str = Field("", validation_alias="OPEN_BASE_URL")
    api_key: SecretStr = Field(
        SecretStr(""), validation_alias="OPEN_API_KEY"
    )

    @property
    def ready(self) -> bool:
        """是否拿到了可用的模型参数（未配置时为 False，用于启动前提示）。"""
        return bool(self.model and self.api_key.get_secret_value())


# 模块级单例：import 时读取一次，改环境变量后要重启进程才生效
model_cfg = ModelConfig()
