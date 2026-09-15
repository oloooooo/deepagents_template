"""登录鉴权路由：注册 / 登录 / 刷新 / 登出 / 当前用户。

约定：路由只收参、调 service、返回；请求/响应模型在 routers/schemas 里。
参数顺序统一为「路径参数 → 请求体 → 当前用户 → session」。
"""

from fastapi import APIRouter, Response, status

from config import app_config
from dependencies import SessionDep
from dependencies.auth import CurrentUser
from models import User
from routers.schemas import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UserOut,
)
from services import AuthService

__all__ = ["router"]

router = APIRouter(prefix="/auth", tags=["auth"])


def _token_pair(access_token: str, refresh_token: str) -> TokenPair:
    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=app_config.auth.access_token_expire_minutes * 60,
    )


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="注册",
)
async def register(payload: RegisterRequest, session: SessionDep) -> User:
    return await AuthService(session).register(
        payload.account, payload.email, payload.password
    )


@router.post("/login", response_model=TokenPair, summary="登录")
async def login(payload: LoginRequest, session: SessionDep) -> TokenPair:
    _, access_token, refresh_token = await AuthService(session).login(
        payload.account, payload.password
    )
    return _token_pair(access_token, refresh_token)


@router.post("/refresh", response_model=TokenPair, summary="用 refresh token 换新令牌")
async def refresh(payload: RefreshRequest, session: SessionDep) -> TokenPair:
    _, access_token, refresh_token = await AuthService(session).refresh(
        payload.refresh_token
    )
    return _token_pair(access_token, refresh_token)


@router.get("/me", response_model=UserOut, summary="当前登录用户")
async def me(current_user: CurrentUser) -> User:
    return current_user


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="登出",
)
async def logout(current_user: CurrentUser, session: SessionDep) -> None:
    await AuthService(session).logout(current_user)
