"""用户-公共空间关联表读写（授权 / 查可见范围 / 撤销）。"""

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from models import PublicWorkspace, User, UserPublicWorkspace

__all__ = ["UserPublicWorkspaceRepository"]


class UserPublicWorkspaceRepository:
    """关联表本身没有业务规则：谁能授权由 service 层判断（只有 super）。

    和 ``UserWorkspaceRepository`` 的关键区别：**没有 permission 列**，所以没有
    「有就更新权限」这回事 —— 重复授权是幂等的 no-op（见 ``docs/adr/0001``）。
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def grant(self, *, user_id: str, public_workspace_id: str) -> None:
        """授权；已存在就什么都不做（幂等，不必先查一遍）。"""
        stmt = insert(UserPublicWorkspace).values(
            user_id=user_id, public_workspace_id=public_workspace_id
        )
        await self.session.execute(
            stmt.on_conflict_do_nothing(
                index_elements=[
                    UserPublicWorkspace.user_id,
                    UserPublicWorkspace.public_workspace_id,
                ]
            )
        )
        await self.session.commit()

    async def revoke(self, *, user_id: str, public_workspace_id: str) -> bool:
        """撤销授权，返回是否真的删掉了（False 说明本来就没关联）。"""
        stmt = delete(UserPublicWorkspace).where(
            UserPublicWorkspace.user_id == user_id,
            UserPublicWorkspace.public_workspace_id == public_workspace_id,
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0  # pyright: ignore[reportAttributeAccessIssue]

    async def has_access(self, *, user_id: str, public_workspace_id: str) -> bool:
        """有没有关联记录。可见范围里「成员」那一半；super 不查这个（见 CONTEXT.md）。"""
        stmt = select(UserPublicWorkspace.id).where(
            UserPublicWorkspace.user_id == user_id,
            UserPublicWorkspace.public_workspace_id == public_workspace_id,
        )
        return await self.session.scalar(stmt) is not None

    async def list_by_user(self, user_id: str) -> list[PublicWorkspace]:
        """某人被授权的公共空间（= 他的可见范围里去掉 super 的那部分）。"""
        stmt = (
            select(PublicWorkspace)
            .join(
                UserPublicWorkspace,
                UserPublicWorkspace.public_workspace_id == PublicWorkspace.id,
            )
            .where(UserPublicWorkspace.user_id == user_id)
            .order_by(PublicWorkspace.name)
        )
        return list(await self.session.scalars(stmt))

    async def list_members(self, public_workspace_id: str) -> list[User]:
        """某公共空间的成员（按加入顺序）。没有权限列可带。"""
        stmt = (
            select(User)
            .join(UserPublicWorkspace, UserPublicWorkspace.user_id == User.id)
            .where(UserPublicWorkspace.public_workspace_id == public_workspace_id)
            .order_by(UserPublicWorkspace.created_at)
        )
        return list(await self.session.scalars(stmt))
