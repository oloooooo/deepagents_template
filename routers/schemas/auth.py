"""注册 / 登录 / 令牌相关的请求 / 响应模型。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "LoginRequest",
    "RefreshRequest",
    "RegisterRequest",
    "TokenPair",
    "UserOut",
]

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
    # 只读暴露：前端据此决定要不要显示「建空间」。没有任何写入口
    is_super: bool
    created_at: datetime
