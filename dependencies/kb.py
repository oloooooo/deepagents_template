"""知识库可见范围：每轮把「这个人能看见哪些微服务」查出来填进 ``AgentContext``。

挂载 backend 是同步的、拿不到数据库 session（与 ``docs/adr/0004`` 同一个道理），
所以可见清单只能在路由层查好、随 ``AgentContext`` 每轮传进去——**不写 checkpoint
metadata**：成员关系会变，落 metadata 的话老会话读到的是过期清单。

清单是 **名字 -> id**（agent 按名字寻址，存储按 id 落）：

- super：全部微服务（``list_all``）——共享层全见；private 层仍然只有自己那份
  （super 看别人的私有走 REST，``/kb/files/read?account=...``）；
- 普通用户：作为成员的那些（``list_for_user``）；一个都不是就给空字典——
  agent 眼里 ``/kb/`` 是空目录，**不是报错**。
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from dependencies.auth import CurrentUser
from dependencies.database import get_session
from repositories import MicroserviceRepository

__all__ = ["KbCellsDep", "get_kb_cells"]


async def get_kb_cells(
    current_user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    repo = MicroserviceRepository(session)
    if current_user.is_super:
        rows = await repo.list_all()
    else:
        rows = await repo.list_for_user(current_user.id)
    return {ms.name: ms.id for ms in rows}


KbCellsDep = Annotated[dict[str, str], Depends(get_kb_cells)]
