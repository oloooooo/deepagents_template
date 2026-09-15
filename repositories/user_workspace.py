"""用户-业务空间关联表读写（授权 / 查权限 / 撤销）。"""

from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from models import User, UserWorkspace, Workspace, WorkspacePermission

__all__ = ["UserWorkspaceRepository"]


class UserWorkspaceRepository:
    """关联表本身没有业务规则：谁能改权限由 service 层判断。

    「有就更新、没有就插入」交给 PG 的 ``ON CONFLICT``，一条语句搞定，
    并发下也不会撞 ``uq_user_workspaces_user_workspace``。
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def grant(
        self, *, user_id: str, workspace_id: str, permission: WorkspacePermission
    ) -> None:
        """授权；已存在同一 (user, workspace) 则改权限。"""
        stmt = insert(UserWorkspace).values(
            user_id=user_id, workspace_id=workspace_id, permission=permission
        )
        await self.session.execute(
            stmt.on_conflict_do_update(
                index_elements=[UserWorkspace.user_id, UserWorkspace.workspace_id],
                # Core 语句不走 ORM 的 onupdate，updated_at 手动带
                set_={
                    "permission": stmt.excluded.permission,
                    "updated_at": datetime.now(timezone.utc),
                },
            )
        )
        await self.session.commit()

    async def get_permission(
        self, *, user_id: str, workspace_id: str
    ) -> WorkspacePermission | None:
        """没关联返回 None，调用方据此决定 403 还是 404。"""
        stmt = select(UserWorkspace.permission).where(
            UserWorkspace.user_id == user_id,
            UserWorkspace.workspace_id == workspace_id,
        )
        return await self.session.scalar(stmt)

    async def list_by_user(
        self, user_id: str
    ) -> list[tuple[Workspace, WorkspacePermission]]:
        """某人能访问的所有空间，附带他在每个空间的权限。"""
        stmt = (
            select(Workspace, UserWorkspace.permission)
            .join(UserWorkspace, UserWorkspace.workspace_id == Workspace.id)
            .where(UserWorkspace.user_id == user_id)
            .order_by(Workspace.created_at.desc())
        )
        rows = await self.session.execute(stmt)
        return [(workspace, permission) for workspace, permission in rows]

    async def list_members(
        self, workspace_id: str
    ) -> list[tuple[User, WorkspacePermission]]:
        """某空间的成员，附带各自权限（按加入顺序）。"""
        stmt = (
            select(User, UserWorkspace.permission)
            .join(UserWorkspace, UserWorkspace.user_id == User.id)
            .where(UserWorkspace.workspace_id == workspace_id)
            .order_by(UserWorkspace.created_at)
        )
        rows = await self.session.execute(stmt)
        return [(user, permission) for user, permission in rows]

    async def revoke(self, *, user_id: str, workspace_id: str) -> bool:
        """撤销授权，返回是否真的删掉了（False 说明本来就没关联）。"""
        stmt = delete(UserWorkspace).where(
            UserWorkspace.user_id == user_id,
            UserWorkspace.workspace_id == workspace_id,
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0  # pyright: ignore[reportAttributeAccessIssue]
