"""Alembic 迁移环境。

连接串不写在 alembic.ini，而是从 config.yaml 的 postgresql.user 段读取，
保证应用与迁移永远用同一份配置。psycopg3 方言同步/异步通用，这里用同步引擎。

注意：checkpoints / store 这些表由 langgraph 的 checkpointer 与 store 自己建（`setup()`），
不归 alembic 管；不排除掉的话 autogenerate 会想把它们删掉。
"""

from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import engine_from_config, pool

from config import app_config
from models import Base

config = context.config

# 不关闭已有 logger：保留 logger 包里 loguru 的日志接管
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# 业务用户库连接串（% 需转义，配置解析器会把 % 当插值符）
config.set_main_option(
    "sqlalchemy.url", app_config.postgresql.user.sqlalchemy_uri.replace("%", "%%")
)

target_metadata = Base.metadata

# langgraph（checkpointer + store）自建自管的表，交给它自己的 setup() 版本管理
LANGGRAPH_TABLES = frozenset(
    {
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
        "checkpoint_migrations",
        "store",
        "store_migrations",
    }
)


def include_object(obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any) -> bool:
    """autogenerate 时忽略 langgraph 的表与它们的索引。"""
    if type_ == "table":
        return name not in LANGGRAPH_TABLES
    if type_ == "index":
        return getattr(getattr(obj, "table", None), "name", None) not in LANGGRAPH_TABLES
    return True


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连库。"""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连库执行迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
