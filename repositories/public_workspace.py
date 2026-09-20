"""公共空间表读写。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import PublicWorkspace

__all__ = ["PublicWorkspaceRepository"]


class PublicWorkspaceRepository:
    """PublicWorkspace 的增删改查，service 层不直接写 SQL。

    ``name`` 唯一且**不可变**（见 ``docs/adr/0002``），所以这里**没有改名字的入口**：
    ``update`` 只改 ``description``。重名由数据库的唯一索引抛 IntegrityError，不预检
    （与 WorkspaceRepository 一致，友好提示交给 service）。
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, public_workspace_id: str) -> PublicWorkspace | None:
        return await self.session.get(PublicWorkspace, public_workspace_id)

    async def get_by_name(self, name: str) -> PublicWorkspace | None:
        return await self.session.scalar(
            select(PublicWorkspace).where(PublicWorkspace.name == name)
        )

    async def list_all(self) -> list[PublicWorkspace]:
        """全部公共空间（= super 的可见范围），按名字排序，方便对照 /public/ 挂载。"""
        return list(
            await self.session.scalars(
                select(PublicWorkspace).order_by(PublicWorkspace.name)
            )
        )

    async def create(self, *, name: str, description: str = "") -> PublicWorkspace:
        workspace = PublicWorkspace(name=name, description=description)
        self.session.add(workspace)
        await self.session.commit()
        await self.session.refresh(workspace)
        return workspace

    async def update(
        self, workspace: PublicWorkspace, *, description: str
    ) -> PublicWorkspace:
        """只改说明。名字不可变，见类 docstring。"""
        workspace.description = description
        await self.session.commit()
        await self.session.refresh(workspace)
        return workspace

    async def delete(self, workspace: PublicWorkspace) -> None:
        await self.session.delete(workspace)
        await self.session.commit()
