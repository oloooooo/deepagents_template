"""公共空间可见范围的依赖：每轮查一次，喂给 ``AgentContext`` 决定 ``/public/`` 挂载。

为什么不写在 checkpoint metadata 里：一个用户可以关联 N 个公共空间，被移出任何一个都必须
**立刻**失效，而 metadata 是会话开始时定死的（见 ``docs/adr/0004``）。所以这里是唯一的查询点，
路由拿到结果往下传，``ChatService`` 不自己查。
"""

from typing import Annotated

from fastapi import Depends

from dependencies import SessionDep
from dependencies.auth import CurrentUser
from services import PublicWorkspaceService

__all__ = ["PublicWorkspaceDep", "get_public_workspaces"]


async def get_public_workspaces(
    current_user: CurrentUser, session: SessionDep
) -> dict[str, str]:
    """当轮可见的公共空间（名字 -> id）。super 是全部，其他人是自己被授权的那些。"""
    return await PublicWorkspaceService(session).visible(current_user)


PublicWorkspaceDep = Annotated[dict[str, str], Depends(get_public_workspaces)]
