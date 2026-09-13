"""FastAPI 依赖（数据库会话、当前用户、智能体）统一从这里导出。"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from .agent import AgentDep, get_agent
from .database import AsyncSessionFactory, engine, get_session

__all__ = [
    "AgentDep",
    "AsyncSessionFactory",
    "SessionDep",
    "engine",
    "get_agent",
    "get_session",
]

# 路由里直接 ``session: SessionDep``
SessionDep = Annotated[AsyncSession, Depends(get_session)]
