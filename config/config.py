"""全局配置加载模块。

用 OmegaConf 读取同目录下的 config.yaml，**先把项目根目录的 .env 灌进环境变量**
（``python-dotenv``），再用 pydantic 校验，最终暴露单例 ``app_config``，
其它模块统一 ``from config import app_config`` 使用。

覆盖优先级（高 → 低）：**系统环境变量 > .env > config.yaml 里的默认值**。

怎么覆盖：在 config.yaml 里用 OmegaConf 插值引用环境变量，第二个参数是默认值::

    password: ${oc.env:PG_PASSWORD,"1234"}
    secret_key: ${oc.env:AUTH_SECRET_KEY,"dev-only-secret-key-change-me-before-prod"}

然后在 ``.env``（已在 .gitignore 里）写 ``PG_PASSWORD=真正的密码`` 即可。

**没有** ``APP_`` 前缀扫描那一套：要覆盖哪个键，就在 yaml 里显式写出来。
好处是「哪些配置能从外部改」在 yaml 里一眼看得见，而不是靠一套隐式命名约定。
"""

import os
from pathlib import Path
from urllib.parse import quote

from typing import Literal

from dotenv import load_dotenv
from omegaconf import DictConfig, OmegaConf
from pydantic import BaseModel, ConfigDict, Field

# config/config.py -> config/ -> 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config" / "config.yaml"
ENV_FILE = BASE_DIR / ".env"

# 先读 .env 再解析 yaml：下面那些 ${oc.env:...} 才有东西可读。
# override=False（默认）：已存在的系统环境变量优先，符合「容器注环境变量、本地用 .env」的直觉。
load_dotenv(ENV_FILE)


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

    # 生产环境务必在 .env 里设 AUTH_SECRET_KEY（见 config.yaml）；HMAC-SHA256 要求至少 32 字节
    secret_key: str = Field(min_length=32, default="dev-only-secret-key-change-me-before-prod")
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7


class DeepAgentConfig(_Strict):
    """deepagents 智能体配置。模型走 OpenAI 兼容端点，因此只用 langchain-openai 一个包。"""

    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    # 从哪个环境变量读 API key（避免把密钥写进 yaml）
    api_key_env: str = "DEEPSEEK_API_KEY"
    temperature: float = 0.0

    @property
    def api_key(self) -> str:
        """读取环境变量里的 API key，缺失时给出明确报错。"""
        key = os.environ.get(self.api_key_env)
        if not key:
            raise RuntimeError(
                f"环境变量 {self.api_key_env} 未设置，无法调用模型 {self.model}；"
                f"可在 config.yaml 的 deepagent.api_key_env 改名字，或设置 APP_DEEPAGENT__API_KEY_ENV 覆盖"
            )
        return key


class KbConfig(_Strict):
    """知识库存储后端（见 docs/adr/0002）：全局默认 + 按微服务名覆盖。

    ``s3`` 在枚举里预留但没有实现——取到它会在 backend 工厂里明确报错，
    不是静默降级。
    """

    backend: Literal["store", "local", "s3"] = "store"
    local_root: Path = Path("data/kb")
    overrides: dict[str, Literal["store", "local", "s3"]] = Field(default_factory=dict)
    """微服务名 -> backend；没配的微服务用全局 ``backend``。"""

    @property
    def resolved_local_root(self) -> Path:
        """local 模式的根目录（相对路径按项目根解析，与 LoggerConfig.log_dir 同规则）。"""
        return self.local_root if self.local_root.is_absolute() else BASE_DIR / self.local_root

    def backend_for(self, microservice_name: str) -> Literal["store", "local", "s3"]:
        """某个微服务最终用哪个 backend（覆盖优先，否则全局默认）。"""
        return self.overrides.get(microservice_name, self.backend)


class AppConfig(_Strict):
    """应用总配置，字段与 config.yaml 的顶层键一一对应。"""

    postgresql: PostgresConfig
    logger: LoggerConfig = LoggerConfig()
    auth: AuthConfig = AuthConfig()
    deepagent: DeepAgentConfig = DeepAgentConfig()
    kb: KbConfig = KbConfig()


def load_config(config_file: Path | str = CONFIG_FILE) -> AppConfig:
    """读取 yaml → pydantic 校验，返回配置对象。

    yaml 里的 ``${oc.env:...}`` 在 ``resolve=True`` 这一步取值，来源是系统环境变量与
    （模块导入时已加载的）``.env``，取不到就用插值的第二个参数（默认值）。
    """
    cfg = OmegaConf.load(config_file)
    if not isinstance(cfg, DictConfig):
        raise TypeError(f"配置文件必须是 YAML 映射结构: {config_file}")
    return AppConfig.model_validate(OmegaConf.to_container(cfg, resolve=True))


# 模块级单例：import 时即完成读取与校验，配置有问题会立刻抛 ValidationError
app_config = load_config()
