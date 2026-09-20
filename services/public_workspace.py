"""公共空间服务：建 / 删 / 授权 + 内容读写。

可见范围（谁能读，见 ``CONTEXT.md`` 的 Visibility）：

- **super**：全部公共空间，不是成员也能读、能写、能删；
- **成员**：只有被授权的那几个，一律只读。

所以这个类里**没有权限等级比较**（公共空间没有 admin / editor / viewer），只有一条
「super 全通 + 成员看关联记录」的判断。管理动作（建 / 删 / 授权 / 撤权 / 成员列表）由路由层的
``SuperUser`` 依赖把关，和 ``WorkspaceService`` 的约定一致。

删除要同时清 store（``docs/adr/0005``）：**先删表并提交，再清内容**，清理失败只记 warning。
反过来一旦表提交失败就是「空间还在、内容已销毁」的真实数据丢失。agent 没起来（没配模型）时
``store`` 是 ``None``，删除照常成功、只留孤儿 —— 孤儿是 ADR-0005 已经接受的。
"""

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from agents.public_workspace import PublicWorkspaceStore
from logger import logger
from models import PublicWorkspace, User
from repositories import (
    PublicWorkspaceRepository,
    UserPublicWorkspaceRepository,
    UserRepository,
)
from services.chat import AGENT_NOT_READY

__all__ = ["PublicWorkspaceService"]

NOT_VISIBLE = "公共空间不存在或你没有访问权限"
NAME_TAKEN = "公共空间名已存在"


class PublicWorkspaceService:
    def __init__(
        self, session: AsyncSession, store: PublicWorkspaceStore | None = None
    ) -> None:
        self.session = session
        self.store = store
        self.workspaces = PublicWorkspaceRepository(session)
        self.links = UserPublicWorkspaceRepository(session)
        self.users = UserRepository(session)

    # ---------- 可见范围 ----------

    async def visible(self, user: User) -> dict[str, str]:
        """当轮可见的公共空间（名字 -> id），喂给 ``AgentContext`` 决定 ``/public/`` 挂载。

        super 是全部，其他人是自己被授权的那些。**每轮查一次**，不落 checkpoint
        （``docs/adr/0004``）—— 被移出公共空间必须立刻失效。
        """
        rows = (
            await self.workspaces.list_all()
            if user.is_super
            else await self.links.list_by_user(user.id)
        )
        return {workspace.name: workspace.id for workspace in rows}

    async def _visible(self, user: User, public_workspace_id: str) -> PublicWorkspace:
        """单个空间的可见性校验；不可见与不存在一律 404，不泄露存在性。"""
        workspace = await self.workspaces.get_by_id(public_workspace_id)
        if workspace is None or not (
            user.is_super
            or await self.links.has_access(
                user_id=user.id, public_workspace_id=public_workspace_id
            )
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_VISIBLE)
        return workspace

    # ---------- 管理（路由层 SuperUser 把关） ----------

    async def create(self, *, name: str, description: str = "") -> PublicWorkspace:
        if await self.workspaces.get_by_name(name) is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NAME_TAKEN)
        return await self.workspaces.create(name=name, description=description)

    async def list_all(self) -> list[PublicWorkspace]:
        """全部公共空间（super 的管理视角）。"""
        return await self.workspaces.list_all()

    async def list_mine(self, user: User) -> list[PublicWorkspace]:
        """**我被授权**的公共空间。

        super 也只看自己被授权的那些 —— 「我是不是成员」和「我能读哪些」是两个问题
        （见 ``CONTEXT.md`` 的 Visibility）。要"我能读的全部"用 ``list_all``。
        """
        return await self.links.list_by_user(user.id)

    async def get(self, user: User, public_workspace_id: str) -> PublicWorkspace:
        return await self._visible(user, public_workspace_id)

    async def update(
        self, public_workspace_id: str, *, description: str
    ) -> PublicWorkspace:
        """改说明。名字不可变（``docs/adr/0002``），要改只能删了重建。"""
        workspace = await self._get_or_404(public_workspace_id)
        return await self.workspaces.update(workspace, description=description)

    async def delete(self, public_workspace_id: str) -> None:
        """先删表并提交，再清 store 内容（``docs/adr/0005``）。"""
        await self.workspaces.delete(await self._get_or_404(public_workspace_id))
        if self.store is None:
            logger.warning(
                "agent 未就绪，公共空间 {} 的内容未清理，留下孤儿（见 ADR-0005）",
                public_workspace_id,
            )
            return
        try:
            deleted = await self.store.adelete_all(public_workspace_id)
        except Exception:  # noqa: BLE001 —— 空间已经删成功了，清理失败不能反过来报失败
            logger.warning(
                "清空公共空间 {} 的 store 内容失败，留下孤儿（见 ADR-0005）",
                public_workspace_id,
                exc_info=True,
            )
            return
        logger.info("公共空间 {} 已删除，顺带清掉 {} 个文件", public_workspace_id, deleted)

    async def grant(self, public_workspace_id: str, *, user_name: str) -> None:
        """授权（幂等：重复授权不报错、不改任何东西）。"""
        await self._get_or_404(public_workspace_id)
        await self.links.grant(
            user_id=(await self._get_user_or_404(user_name)).id,
            public_workspace_id=public_workspace_id,
        )

    async def revoke(self, public_workspace_id: str, user_name: str) -> None:
        await self._get_or_404(public_workspace_id)
        user = await self._get_user_or_404(user_name)
        if not await self.links.revoke(
            user_id=user.id, public_workspace_id=public_workspace_id
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="该用户不在这个公共空间"
            )

    async def list_members(self, public_workspace_id: str) -> list[User]:
        await self._get_or_404(public_workspace_id)
        return await self.links.list_members(public_workspace_id)

    # ---------- 内容 ----------

    async def list_files(self, user: User, public_workspace_id: str) -> list[str]:
        await self._visible(user, public_workspace_id)
        return await self._require_store().alist(public_workspace_id)

    async def read_file(self, user: User, public_workspace_id: str, path: str) -> str:
        await self._visible(user, public_workspace_id)
        content = await self._require_store().aread(public_workspace_id, path)
        if content is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"公共空间里没有 {path}",
            )
        return content

    async def write_file(
        self, public_workspace_id: str, path: str, content: str
    ) -> str:
        """写 / 覆盖一份内容。能走到这里的一定是 super（路由层 ``SuperUser``）。"""
        await self._get_or_404(public_workspace_id)
        return await self._require_store().awrite(public_workspace_id, path, content)

    async def delete_file(self, public_workspace_id: str, path: str) -> None:
        await self._get_or_404(public_workspace_id)
        if not await self._require_store().adelete(public_workspace_id, path):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"公共空间里没有 {path}",
            )

    # ---------- 内部 ----------

    def _require_store(self) -> PublicWorkspaceStore:
        """内容读写只能走 store，所以必须 agent 已就绪（和 ``/memories/*`` 一样）。"""
        if self.store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=AGENT_NOT_READY
            )
        return self.store

    async def _get_or_404(self, public_workspace_id: str) -> PublicWorkspace:
        workspace = await self.workspaces.get_by_id(public_workspace_id)
        if workspace is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="公共空间不存在"
            )
        return workspace

    async def _get_user_or_404(self, user_name: str) -> User:
        """按账号名取用户（授权 / 撤权的目标），不存在统一 404。"""
        user = await self.users.get_by_name(user_name)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在"
            )
        return user
