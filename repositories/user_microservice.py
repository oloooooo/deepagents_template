"""用户-微服务成员关系读写（授权 / 收权 / 查成员）。"""

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import UserMicroservice

__all__ = ["UserMicroserviceRepository"]


class UserMicroserviceRepository:
    """成员关系的增删查。关系是二元的，没有权限列（见 docs/adr/0001）。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, *, user_id: str, microservice_id: str) -> UserMicroservice | None:
        return await self.session.scalar(
            select(UserMicroservice).where(
                UserMicroservice.user_id == user_id,
                UserMicroservice.microservice_id == microservice_id,
            )
        )

    async def is_member(self, *, user_id: str, microservice_id: str) -> bool:
        return await self.get(user_id=user_id, microservice_id=microservice_id) is not None

    async def add(self, *, user_id: str, microservice_id: str) -> UserMicroservice:
        """授权。已是成员时返回已有记录（幂等，service 层据此判断 200/409）。"""
        existing = await self.get(user_id=user_id, microservice_id=microservice_id)
        if existing is not None:
            return existing
        member = UserMicroservice(user_id=user_id, microservice_id=microservice_id)
        self.session.add(member)
        await self.session.commit()
        await self.session.refresh(member)
        return member

    async def remove(self, *, user_id: str, microservice_id: str) -> bool:
        """收权，返回是否真删了一条。"""
        result = await self.session.execute(
            delete(UserMicroservice).where(
                UserMicroservice.user_id == user_id,
                UserMicroservice.microservice_id == microservice_id,
            )
        )
        await self.session.commit()
        return result.rowcount > 0

    async def list_member_ids(self, microservice_id: str) -> list[str]:
        """某微服务的全部成员 id（REST 列成员用）。"""
        stmt = select(UserMicroservice.user_id).where(
            UserMicroservice.microservice_id == microservice_id
        )
        return list(await self.session.scalars(stmt))
