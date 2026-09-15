"""全局配置加载模块。

用 OmegaConf 读取同目录下的 config.yaml，合并环境变量后用 pydantic 校验，
最终暴露单例 ``app_config``，其它模块统一 ``from config import app_config`` 使用。

覆盖优先级（高 → 低）：环境变量 > config.yaml。
环境变量前缀 ``APP_``，层级用 ``__`` 分隔，且只能覆盖 yaml 里已存在的键::

    APP_POSTGRESQL__USER__PASSWORD=secret
    APP_AUTH__SECRET_KEY=xxx

也可以在 yaml 里用 OmegaConf 插值引用环境变量::

    secret_key: ${oc.env:MY_SECRET_KEY,dev-only-secret-key-change-me-before-prod}
"""

import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote

from omegaconf import DictConfig, OmegaConf
from pydantic import BaseModel, ConfigDict, Field

# config/config.py -> config/ -> 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config" / "config.yaml"

# 环境变量约定
ENV_PREFIX = "APP_"
ENV_NESTED_DELIMITER = "__"


class _Strict(BaseModel):
    """所有配置模型的基类：写错键名直接报错，而不是被静默忽略。"""

    model_config = ConfigDict(extra="forbid")


class PostgreConfig(_Strict):
    """单个 PostgreSQL 库的连接参数。"""

    host: str = "127.0.0.1"
    port: int = 5432
    user: str
    password: str
    db_name: str

    @property
    def uri(self) -> str:
        """psycopg 原生连接串（供 psycopg_pool / langgraph checkpointer 使用）。"""
        return (
            f"postgresql://{quote(self.user, safe='')}:{quote(self.password, safe='')}"
            f"@{self.host}:{self.port}/{self.db_name}"
        )

    @property
    def sqlalchemy_uri(self) -> str:
        """SQLAlchemy 连接串。

        psycopg3 方言同时支持同步（alembic）与异步（FastAPI）引擎，无需区分。
        """
        return (
            f"postgresql+psycopg://{quote(self.user, safe='')}:{quote(self.password, safe='')}"
            f"@{self.host}:{self.port}/{self.db_name}"
        )


class PostgresConfig(_Strict):
    """postgresql 段：业务库与 deepagents 库分开配置，将来拆库只改 yaml。"""

    # deepagents 的 state（检查点）与 store（长期记忆）
    # deepagent: PostgreConfig
    # 业务用户库（用户表、登录鉴权）
    user: PostgreConfig


class LoggerConfig(_Strict):
    """loguru 日志配置。"""

    level: str = "INFO"
    # 日志目录，相对路径按项目根目录解析
    dir: Path = Path("logs")
    # 单个文件的大小/时间切分条件，loguru 语法，如 "10 MB" / "00:00"
    rotation: str = "10 MB"
    # 历史日志保留时长，如 "7 days"
    retention: str = "7 days"
    # 异常是否打印完整堆栈
    backtrace: bool = True
    # 异常是否附带变量值（生产环境建议关闭，避免泄漏敏感数据）
    diagnose: bool = False

    @property
    def log_dir(self) -> Path:
        """日志目录的绝对路径。"""
        return self.dir if self.dir.is_absolute() else BASE_DIR / self.dir


class AuthConfig(_Strict):
    """用户登录与鉴权（JWT）配置。"""

    # 生产环境务必用 APP_AUTH__SECRET_KEY 覆盖；HMAC-SHA256 要求至少 32 字节
    secret_key: str = Field(min_length=32, default="dev-only-secret-key-change-me-before-prod")
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7


class AppConfig(_Strict):
    """应用总配置，字段与 config.yaml 的顶层键一一对应。"""

    postgresql: PostgresConfig
    logger: LoggerConfig = LoggerConfig()
    auth: AuthConfig = AuthConfig()


def _apply_env_overrides(cfg: DictConfig, env: Mapping[str, str] | None = None) -> None:
    """把 ``APP_`` 前缀的环境变量写进 OmegaConf 配置。

    只覆盖 yaml 里已有的键：无关的 APP_* 环境变量不会污染配置（否则会被
    pydantic 的 extra="forbid" 拒收）。值按字符串写入，类型交给 pydantic 转换。
    """
    for key, value in (os.environ if env is None else env).items():
        if not key.startswith(ENV_PREFIX):
            continue
        path = ".".join(
            part.lower() for part in key[len(ENV_PREFIX) :].split(ENV_NESTED_DELIMITER)
        )
        if OmegaConf.select(cfg, path, throw_on_missing=False) is None:
            continue
        OmegaConf.update(cfg, path, value, merge=False)


def load_config(config_file: Path | str = CONFIG_FILE) -> AppConfig:
    """读取 yaml → 应用环境变量覆盖 → pydantic 校验，返回配置对象。"""
    cfg = OmegaConf.load(config_file)
    if not isinstance(cfg, DictConfig):
        raise TypeError(f"配置文件必须是 YAML 映射结构: {config_file}")
    _apply_env_overrides(cfg)
    return AppConfig.model_validate(OmegaConf.to_container(cfg, resolve=True))


# 模块级单例：import 时即完成读取与校验，配置有问题会立刻抛 ValidationError
app_config = load_config()
