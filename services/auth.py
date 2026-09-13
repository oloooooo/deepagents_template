"""认证服务：密码哈希、JWT 签发与校验、注册 / 登录 / 刷新 / 登出。"""

import hmac
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import bcrypt
import jwt
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from config import app_config
from models import User
from repositories import UserRepository

__all__ = [
    "ACCESS_TOKEN_TYPE",
    "REFRESH_TOKEN_TYPE",
    "AuthService",
    "decode_token",
    "hash_password",
    "verify_password",
]

# bcrypt 成本因子，hash 字符串固定 60 字节
BCRYPT_ROUNDS = 12
# bcrypt 只处理前 72 字节，超长直接拒绝，避免"静默截断"导致的弱密码
MAX_PASSWORD_BYTES = 72

ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"


def _encode_password(password: str) -> bytes:
    raw = password.encode("utf-8")
    if len(raw) > MAX_PASSWORD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"密码不能超过 {MAX_PASSWORD_BYTES} 字节",
        )
    return raw


def hash_password(password: str) -> str:
    """bcrypt 加盐哈希。"""
    return bcrypt.hashpw(_encode_password(password), bcrypt.gensalt(BCRYPT_ROUNDS)).decode()


def verify_password(password: str, hashed_password: str) -> bool:
    """校验明文密码；哈希串损坏时按不匹配处理，不抛 500。"""
    try:
        return bcrypt.checkpw(_encode_password(password), hashed_password.encode())
    except ValueError:
        return False


def create_token(user_id: str, token_type: str, expires: timedelta) -> str:
    """签发一个 JWT：sub=用户 id，type 区分 access/refresh。"""
    cfg = app_config.auth
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "type": token_type,
        "jti": uuid4().hex,
        "iat": now,
        "exp": now + expires,
    }
    return jwt.encode(payload, cfg.secret_key, algorithm=cfg.algorithm)


def decode_token(token: str, expected_type: str) -> str:
    """校验签名/有效期/类型，返回 sub（用户 id）；任何问题统一 401。"""
    cfg = app_config.auth
    try:
        payload = jwt.decode(token, cfg.secret_key, algorithms=[cfg.algorithm])
    except jwt.PyJWTError:
        payload = None
    if payload is None or payload.get("type") != expected_type or not payload.get("sub"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效或已过期的令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload["sub"]


class AuthService:
    """路由层只管收参/返回，鉴权逻辑全在这里。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.users = UserRepository(session)

    async def register(self, account: str, email: str, password: str) -> User:
        if await self.users.get_by_account(account) or await self.users.get_by_account(email):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="账号或邮箱已被注册"
            )
        return await self.users.create(
            account=account, email=email, hashed_password=hash_password(password)
        )

    async def authenticate(self, account: str, password: str) -> User:
        """账号（或邮箱）+ 密码换用户，失败信息不区分账号不存在/密码错误。"""
        user = await self.users.get_by_account(account)
        if user is None or not verify_password(password, user.hashed_password):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="账号或密码错误",
                headers={"WWW-Authenticate": "Bearer"},
            )
        self._ensure_active(user)
        return user

    async def login(self, account: str, password: str) -> tuple[User, str, str]:
        """返回 (用户, access_token, refresh_token)，refresh token 落库。"""
        user = await self.authenticate(account, password)
        return (user, *await self._issue_tokens(user))

    async def refresh(self, refresh_token: str) -> tuple[User, str, str]:
        """用 refresh token 换新的一对令牌，并轮换（旧 refresh token 立即失效）。"""
        user = await self.users.get_by_id(decode_token(refresh_token, REFRESH_TOKEN_TYPE))
        # 与库中保存的 token 比对：登出、被轮换过的旧 token 一律拒绝
        if (
            user is None
            or not user.refresh_token
            or not hmac.compare_digest(user.refresh_token, refresh_token)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="refresh token 已失效，请重新登录",
                headers={"WWW-Authenticate": "Bearer"},
            )
        self._ensure_active(user)
        return (user, *await self._issue_tokens(user))

    async def logout(self, user: User) -> None:
        """清空 refresh token；access token 有效期短，靠过期自然失效。"""
        await self.users.update_refresh_token(user, None)

    async def _issue_tokens(self, user: User) -> tuple[str, str]:
        cfg = app_config.auth
        access_token = create_token(
            user.id, ACCESS_TOKEN_TYPE, timedelta(minutes=cfg.access_token_expire_minutes)
        )
        refresh_token = create_token(
            user.id, REFRESH_TOKEN_TYPE, timedelta(days=cfg.refresh_token_expire_days)
        )
        await self.users.update_refresh_token(user, refresh_token)
        return access_token, refresh_token

    @staticmethod
    def _ensure_active(user: User) -> None:
        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="用户已被禁用"
            )
