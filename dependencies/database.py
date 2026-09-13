"""FastAPI 数据库依赖：引擎 / 会话工厂 / 请求级 Session。"""

import asyncio
import sys
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import app_config

__all__ = ["AsyncSessionFactory", "engine", "get_session"]

if sys.platform == "win32":
    # Windows 默认的 ProactorEventLoop 与 psycopg 异步驱动不兼容
    # （psycopg.InterfaceError: cannot use the 'ProactorEventLoop'），
    # 而 uvicorn 非 reload 模式在 win32 上恰好选 Proactor，因此在这里统一换成 Selector。
    # ponytail: 事件循环策略 API 将在 Python 3.16 移除，届时改用 uvicorn 的 loop_factory。
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 业务用户库（config.yaml 的 postgresql.user 段）异步引擎
engine = create_async_engine(
    app_config.postgresql.user.sqlalchemy_uri,
    # 是否打印所有 SQL（默认关闭）
    echo=False,
    # 连接池大小
    pool_size=10,
    # 连接池允许的最大连接数
    max_overflow=20,
    # 获取连接超时时间（秒）
    pool_timeout=10,
    # 连接回收时间（秒）
    pool_recycle=3600,
    # 取连接前先探活，避免用到期断开的长连接
    pool_pre_ping=True,
)

AsyncSessionFactory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    autoflush=True,
    expire_on_commit=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """每个请求一个会话，请求结束自动关闭。"""
    async with AsyncSessionFactory() as session:
        yield session
