"""鉴权依赖：从 Authorization: Bearer <token> 解析当前登录用户。"""

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.database import get_session
from models import User
from repositories import UserRepository
from services import ACCESS_TOKEN_TYPE, decode_token

__all__ = ["CurrentUser", "bearer_scheme", "get_current_user"]

# auto_error=False：没带令牌时自己抛 401，错误信息统一
bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    """路由里 ``current_user: CurrentUser`` 即可拿到已登录用户。"""
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无效或已过期的令牌",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise unauthorized

    user = await UserRepository(session).get_by_id(
        decode_token(credentials.credentials, ACCESS_TOKEN_TYPE)
    )
    if user is None:
        raise unauthorized
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="用户已被禁用"
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
