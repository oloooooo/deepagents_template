"""用户表读写。"""

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import User

__all__ = ["UserRepository"]


class UserRepository:
    """所有 User 的增删改查都在这里，上层 service 不直接写 SQL。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, user_id: str) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_account(self, account: str) -> User | None:
        """account 参数可以是登录账号，也可以是邮箱。"""
        stmt = select(User).where(or_(User.account == account, User.email == account))
        return await self.session.scalar(stmt)

    async def create(self, *, account: str, email: str, hashed_password: str) -> User:
        user = User(account=account, email=email, hashed_password=hashed_password)
        self.session.add(user)
        await self.session.commit()
        await self.session.refresh(user)
        return user

    async def update_refresh_token(self, user: User, refresh_token: str | None) -> None:
        """写入或清空 refresh token（登录、刷新、登出时调用）。"""
        user.refresh_token = refresh_token
        await self.session.commit()
