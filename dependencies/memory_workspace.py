"""记忆树格子清单的依赖：每轮查一次，喂给 ``AgentContext.memory_workspaces``。

和 ``dependencies/public_workspace.py`` 同一个理由（``docs/adr/0004``）：一个用户可以参与 N 个
空间，被移出任何一个都必须**立刻**失效，而 metadata 是会话开始时定死的。所以这里是唯一的
查询点，路由拿到结果往下传，``ChatService`` 不自己查。

清单 = 成员关系 + 虚拟 ``default``（``WorkspaceService.visible``），与 ``GET /memories/all``
同源：agent 能看见的格子，和用户能在接口里列出来的，永远是同一份。
"""

from typing import Annotated

from fastapi import Depends

from dependencies import SessionDep
from dependencies.auth import CurrentUser
from services import WorkspaceService

__all__ = ["MemoryWorkspaceDep", "get_memory_workspaces"]


async def get_memory_workspaces(
    current_user: CurrentUser, session: SessionDep
) -> dict[str, str]:
    """当轮可见的业务空间（名字 -> id），含虚拟 default。"""
    return await WorkspaceService(session).visible(current_user)


MemoryWorkspaceDep = Annotated[dict[str, str], Depends(get_memory_workspaces)]
