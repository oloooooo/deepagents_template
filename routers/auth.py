"""登录鉴权路由：注册 / 登录 / 刷新 / 登出 / 当前用户。"""

from datetime import datetime

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, ConfigDict, Field

from config import app_config
from dependencies import SessionDep
from dependencies.auth import CurrentUser
from models import User
from services import AuthService

__all__ = ["router"]

router = APIRouter(prefix="/auth", tags=["auth"])

# 密码最短 8 位；这里不引 email-validator，用正则做基础格式校验
PASSWORD_MIN_LENGTH = 8
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class RegisterRequest(BaseModel):
    account: str = Field(min_length=3, max_length=50, pattern=r"^[A-Za-z0-9_.-]+$")
    email: str = Field(max_length=255, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=72)


class LoginRequest(BaseModel):
    # 账号或邮箱都行
    account: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=72)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="access_token 有效期（秒）")


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    account: str
    email: str
    is_active: bool
    created_at: datetime


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


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="登出",
)
async def logout(current_user: CurrentUser, session: SessionDep) -> None:
    await AuthService(session).logout(current_user)


@router.get("/me", response_model=UserOut, summary="当前登录用户")
async def me(current_user: CurrentUser) -> User:
    return current_user
