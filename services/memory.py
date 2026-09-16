"""长期记忆服务：鉴权 + 调 ``AgentMemory`` 读写 store。

规则（与 ``agents/readme.md`` 一致）：

- 记忆库按 ``(user_id, workspace_id)`` 隔离，``user_id`` 只能来自登录态；
- 读要 viewer 起，写/删要 editor / admin 起（校验都在 :class:`WorkspaceAccess`）；
- 路径校验的 ValueError 一律转 422，别让它冒成 500。
"""

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from agents.agent import AgentMemory
from models import User
from services.access import WorkspaceAccess

__all__ = ["MEMORY_NOT_FOUND", "MemoryService"]

MEMORY_NOT_FOUND = "记忆不存在"


class MemoryService:
    def __init__(self, session: AsyncSession, memory: AgentMemory) -> None:
        self.memory = memory
        self.access = WorkspaceAccess(session)

    async def list_memories(self, user: User, workspace_id: str) -> list[str]:
        await self.access.permission(user, workspace_id)
        return await self._call(self.memory.alist_memories, user.id, workspace_id)

    async def read(self, user: User, workspace_id: str, path: str) -> str:
        await self.access.permission(user, workspace_id)
        content = await self._call(
            self.memory.aread_memory, user.id, workspace_id, path
        )
        if content is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=MEMORY_NOT_FOUND
            )
        return content

    async def write(
        self, user: User, workspace_id: str, path: str, content: str
    ) -> str:
        await self.access.require_writer(user, workspace_id)
        return await self._call(
            self.memory.awrite_memory, user.id, workspace_id, path, content
        )

    async def delete(self, user: User, workspace_id: str, path: str) -> None:
        await self.access.require_writer(user, workspace_id)
        if not await self._call(self.memory.adelete_memory, user.id, workspace_id, path):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=MEMORY_NOT_FOUND
            )

    @staticmethod
    async def _call(fn: Callable[..., Awaitable[Any]], *args: Any) -> Any:
        """调 AgentMemory：它用 ValueError 拒绝非法路径，这里换成 422。"""
        try:
            return await fn(*args)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
