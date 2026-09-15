"""业务空间服务：建空间、增删改查、成员授权与撤销。

权限规则：
- **写**（建/改/删空间、增删成员）由路由层的 ``SuperUser`` 依赖把关，
  ``users.is_super`` 只能在数据库里改，没有 repository/API 写入口；
- **读**（空间详情 / 我参与的空间 / 成员列表）：是成员就行；
- 不是成员一律按「空间不存在」返回 404，不泄露空间是否存在；
- ``user_workspaces.permission``（admin/editor/viewer）现在只描述成员身份，
  不参与鉴权，留给以后空间内的功能（跑 agent、写文件等）用。

所以这个类里只有读权限校验，没有 super 判断。
"""

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from models import User, UserWorkspace, Workspace, WorkspacePermission
from repositories import UserRepository, UserWorkspaceRepository, WorkspaceRepository

__all__ = ["WorkspaceService"]

NOT_MEMBER = "空间不存在或你不是该空间成员"
WORKSPACE_NOT_FOUND = "空间不存在"


class WorkspaceService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.workspaces = WorkspaceRepository(session)
        self.links = UserWorkspaceRepository(session)
        self.users = UserRepository(session)

    async def create(self, owner: User, *, name: str, path: str) -> Workspace:
        """建空间，建的人同时成为该空间 admin，一个事务提交。"""
        if await self.workspaces.get_by_name(name) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="空间名已存在"
            )
        workspace = Workspace(name=name, path=path)
        self.session.add(workspace)
        # 先 flush 拿到 workspace.id（主键是 Python 侧生成的 uuid）
        await self.session.flush()
        self.session.add(
            UserWorkspace(
                user_id=owner.id,
                workspace_id=workspace.id,
                permission=WorkspacePermission.ADMIN,
            )
        )
        await self.session.commit()
        # commit 会让对象过期，异步下再读属性会 MissingGreenlet，必须 refresh
        await self.session.refresh(workspace)
        return workspace

    async def get(self, user: User, workspace_id: str) -> Workspace:
        workspace, _ = await self._access(workspace_id, user.id)
        return workspace

    async def list_mine(
        self, user: User
    ) -> list[tuple[Workspace, WorkspacePermission]]:
        return await self.links.list_by_user(user.id)

    async def update(
        self,
        workspace_id: str,
        *,
        name: str | None = None,
        path: str | None = None,
    ) -> Workspace:
        workspace = await self._get_or_404(workspace_id)
        if name is not None and name != workspace.name:
            if await self.workspaces.get_by_name(name) is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="空间名已存在"
                )
        return await self.workspaces.update(workspace, name=name, path=path)

    async def delete(self, workspace_id: str) -> None:
        """硬删：成员关联由外键级联清理。"""
        await self.workspaces.delete(await self._get_or_404(workspace_id))

    async def list_members(
        self, user: User, workspace_id: str
    ) -> list[tuple[User, WorkspacePermission]]:
        await self._access(workspace_id, user.id)
        return await self.links.list_members(workspace_id)

    async def grant_member(
        self,
        workspace_id: str,
        *,
        user_name: str,
        permission: WorkspacePermission,
    ) -> None:
        """按账号名加成员或改权限（同一个人重复授权即更新）。"""
        await self._get_or_404(workspace_id)
        await self.links.grant(
            user_id=(await self._get_user_or_404(user_name)).id,
            workspace_id=workspace_id,
            permission=permission,
        )

    async def revoke_member(self, workspace_id: str, user_name: str) -> None:
        await self._get_or_404(workspace_id)
        user = await self._get_user_or_404(user_name)
        if not await self.links.revoke(user_id=user.id, workspace_id=workspace_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="该用户不在这个空间"
            )

    async def _get_or_404(self, workspace_id: str) -> Workspace:
        workspace = await self.workspaces.get_by_id(workspace_id)
        if workspace is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=WORKSPACE_NOT_FOUND
            )
        return workspace

    async def _get_user_or_404(self, user_name: str) -> User:
        """按账号名取用户（写接口的目标用户），不存在统一 404。

        内部仍拿 id 去写 user_workspaces：外键指向主键，用名字只影响入参。
        """
        user = await self.users.get_by_name(user_name)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在"
            )
        return user

    async def _access(
        self, workspace_id: str, user_id: str
    ) -> tuple[Workspace, WorkspacePermission]:
        """读权限校验：没关联按 404 处理。"""
        permission = await self.links.get_permission(
            user_id=user_id, workspace_id=workspace_id
        )
        workspace = (
            await self.workspaces.get_by_id(workspace_id)
            if permission is not None
            else None
        )
        if workspace is None or permission is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=NOT_MEMBER
            )
        return workspace, permission
