"""微服务管理服务：建 / 删 / 列 / 授权（super 的动作），见 docs/adr/0001。

删库的顺序是硬规矩：**先删表记录，再清存储内容**（``purge`` 内部失败只记日志）——
反过来的话清内容失败会留下一行指向空内容的记录，比残留文件更难排查。

清内容要 ``store``（agent 持有的连接）：拿不到（agent 未启动）时路由层已经 503 拦住，
所以这里 ``store`` 永远非空；``local`` 目录的清理不依赖它。
"""

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from agents.kb.storage import purge
from models import Microservice, User
from repositories import MicroserviceRepository, UserMicroserviceRepository, UserRepository

__all__ = [
    "MS_NOT_FOUND",
    "MicroserviceService",
    "NAME_TAKEN",
    "USER_NOT_FOUND",
]

MS_NOT_FOUND = "微服务不存在"
NAME_TAKEN = "微服务名已存在"
USER_NOT_FOUND = "用户不存在"


class MicroserviceService:
    def __init__(self, session: AsyncSession, store=None) -> None:
        self.microservices = MicroserviceRepository(session)
        self.members = UserMicroserviceRepository(session)
        self.users = UserRepository(session)
        self.store = store

    async def create(self, *, name: str, description: str | None) -> Microservice:
        """创建微服务 = 创建它的知识库空间（重名 409）。"""
        if await self.microservices.get_by_name(name) is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=NAME_TAKEN)
        return await self.microservices.create(name=name, description=description)

    async def delete(self, microservice_id: str) -> None:
        """删除微服务 = 删除知识库：表记录先行，内容随后（失败只记日志）。"""
        ms = await self.microservices.get_by_id(microservice_id)
        if ms is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=MS_NOT_FOUND)
        await self.microservices.delete(ms)
        await purge(self.store, microservice_id=ms.id, name=ms.name)

    async def list(self, user: User) -> list[Microservice]:
        """我能读哪些：super 全部，普通用户按成员关系。"""
        if user.is_super:
            return await self.microservices.list_all()
        return await self.microservices.list_for_user(user.id)

    async def mine(self, user: User) -> list[Microservice]:
        """我是不是成员：纯成员关系，super 没有授权记录时就是空的（与 list 是两个问题）。"""
        return await self.microservices.list_for_user(user.id)

    async def grant(self, microservice_id: str, account: str) -> None:
        ms = await self._ms(microservice_id)
        target = await self.users.get_by_name(account)
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=USER_NOT_FOUND)
        # 幂等：已是成员返回 200 而不是报错
        await self.members.add(user_id=target.id, microservice_id=ms.id)

    async def revoke(self, microservice_id: str, account: str) -> None:
        ms = await self._ms(microservice_id)
        target = await self.users.get_by_name(account)
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=USER_NOT_FOUND)
        # 幂等：本来就不在成员里，也是 200
        await self.members.remove(user_id=target.id, microservice_id=ms.id)

    async def _ms(self, microservice_id: str) -> Microservice:
        ms = await self.microservices.get_by_id(microservice_id)
        if ms is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=MS_NOT_FOUND)
        return ms
