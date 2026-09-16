"""空间权限校验：记忆与聊天服务共用的唯一入口。

- ``user_id`` 只来自登录态，这里只回答「这个人在这个空间里是什么权限」；
- 不是成员、或空间不存在，一律 404（两种情况不区分，不泄露空间是否存在）；
- 在空间里但权限不够才 403（藏不住，他在空间里）。
"""

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from models import User, WorkspacePermission
from repositories import UserWorkspaceRepository, WorkspaceRepository

__all__ = ["CANNOT_WRITE", "NOT_MEMBER", "WRITERS", "WorkspaceAccess"]

NOT_MEMBER = "空间不存在或你不是该空间成员"
CANNOT_WRITE = "只有空间编辑者（editor）或管理员（admin）能修改记忆"

WRITERS = (WorkspacePermission.EDITOR, WorkspacePermission.ADMIN)


class WorkspaceAccess:
    def __init__(self, session: AsyncSession) -> None:
        self.workspaces = WorkspaceRepository(session)
        self.links = UserWorkspaceRepository(session)

    async def permission(self, user: User, workspace_id: str) -> WorkspacePermission:
        """我在这空间的权限；非成员 / 空间不存在统一 404。"""
        workspace = await self.workspaces.get_by_id(workspace_id)
        permission = (
            await self.links.get_permission(user_id=user.id, workspace_id=workspace.id)
            if workspace is not None
            else None
        )
        if permission is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=NOT_MEMBER
            )
        return permission

    async def require_writer(self, user: User, workspace_id: str) -> WorkspacePermission:
        """写操作：viewer 403。"""
        permission = await self.permission(user, workspace_id)
        if permission not in WRITERS:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=CANNOT_WRITE
            )
        return permission
