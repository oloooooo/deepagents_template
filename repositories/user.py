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

    async def get_by_name(self, name: str) -> User | None:
        """按账号名精确匹配。

        授权/踢人接口用这个：``get_by_account`` 是「账号或邮箱」语义，
        万一 A 的账号恰好等于 B 的邮箱，返回哪条取决于数据库，不适合当授权目标。
        """
        return await self.session.scalar(select(User).where(User.account == name))

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
