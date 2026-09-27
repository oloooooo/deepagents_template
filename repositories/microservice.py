"""微服务表读写。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Microservice, UserMicroservice

__all__ = ["MicroserviceRepository"]


class MicroserviceRepository:
    """微服务的增删查 + 成员可见范围查询。授权（成员关系写入）在 UserMicroserviceRepository。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, microservice_id: str) -> Microservice | None:
        return await self.session.get(Microservice, microservice_id)

    async def get_by_name(self, name: str) -> Microservice | None:
        return await self.session.scalar(
            select(Microservice).where(Microservice.name == name)
        )

    async def list_all(self) -> list[Microservice]:
        """全部微服务（super 的可见范围）。"""
        result = await self.session.scalars(
            select(Microservice).order_by(Microservice.name)
        )
        return list(result)

    async def list_for_user(self, user_id: str) -> list[Microservice]:
        """这个用户作为成员的微服务（普通用户的可见范围，成员 ∩ 全部）。"""
        stmt = (
            select(Microservice)
            .join(UserMicroservice, UserMicroservice.microservice_id == Microservice.id)
            .where(UserMicroservice.user_id == user_id)
            .order_by(Microservice.name)
        )
        return list(await self.session.scalars(stmt))

    async def create(self, *, name: str, description: str | None = None) -> Microservice:
        microservice = Microservice(name=name, description=description)
        self.session.add(microservice)
        await self.session.commit()
        await self.session.refresh(microservice)
        return microservice

    async def delete(self, microservice: Microservice) -> None:
        """删微服务：成员关系靠外键 CASCADE 消失，存储内容由 service 层清（先表后内容）。"""
        await self.session.delete(microservice)
        await self.session.commit()

    async def update_description(self, microservice: Microservice, description: str | None) -> None:
        microservice.description = description
        await self.session.commit()
        await self.session.refresh(microservice)
