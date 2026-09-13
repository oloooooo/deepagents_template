"""FastAPI 依赖（数据库会话、当前用户）统一从这里导出。"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from .database import AsyncSessionFactory, engine, get_session

__all__ = ["AsyncSessionFactory", "SessionDep", "engine", "get_session"]

# 路由里直接 ``session: SessionDep``
SessionDep = Annotated[AsyncSession, Depends(get_session)]
