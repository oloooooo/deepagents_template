"""业务空间表读写。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Workspace

__all__ = ["WorkspaceRepository"]


class WorkspaceRepository:
    """Workspace 的增删改查，service 层不直接写 SQL。

    ``name`` 有唯一索引，重复会由数据库抛 IntegrityError，这里不预检
    （与 UserRepository 一致，是否友好提示交给 service 决定）。
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, workspace_id: str) -> Workspace | None:
        return await self.session.get(Workspace, workspace_id)

    async def get_by_name(self, name: str) -> Workspace | None:
        return await self.session.scalar(select(Workspace).where(Workspace.name == name))

    async def list_all(self, *, offset: int = 0, limit: int = 100) -> list[Workspace]:
        stmt = (
            select(Workspace)
            .order_by(Workspace.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(await self.session.scalars(stmt))

    async def create(self, *, name: str, path: str) -> Workspace:
        workspace = Workspace(name=name, path=path) 
        self.session.add(workspace)
        await self.session.commit()
        await self.session.refresh(workspace)
        return workspace

    async def update(
        self, workspace: Workspace, *, name: str | None = None, path: str | None = None
    ) -> Workspace:
        """只改传进来的字段，None 表示不动（清空请传空字符串）。"""
        if name is not None:
            workspace.name = name
        if path is not None:
            workspace.path = path
        await self.session.commit()
        await self.session.refresh(workspace)
        return workspace

    async def delete(self, workspace: Workspace) -> None:
        await self.session.delete(workspace)
        await self.session.commit()
