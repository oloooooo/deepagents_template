"""长期记忆服务：鉴权 + 调 ``AgentMemory`` 读写 store。

规则（与 ``agents/readme.md`` 一致）：

- 记忆库按 ``(user_id, workspace_id)`` 隔离，``user_id`` 只能来自登录态；
- 读要 viewer 起，写 / 删 / 上传要 editor / admin 起（校验都在 :class:`WorkspaceAccess`）；
- 路径校验的 ValueError 一律转 422，别让它冒成 500。

**路径在响应里是 agent 眼里的写法**（``/memories/{空间名}/notes/a.md``）：REST 按 id 寻址、
agent 按名字寻址，两边在响应里对齐，用户拿到的路径可以原样丢给 agent（``docs/adr/0009``）。
请求里的路径三种写法都收：``notes/a.md`` / ``/memories/notes/a.md`` / ``/memories/{该空间名}/notes/a.md``。
"""

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from agents.agent import MEMORY_ROUTE, AgentMemory
from models import User
from services.access import WorkspaceAccess
from services.workspace import WorkspaceService

__all__ = ["MEMORY_NOT_FOUND", "MemoryService"]

MEMORY_NOT_FOUND = "记忆不存在"
TEXT_ONLY = "只收 UTF-8 文本文件（二进制请另行存放）"
UPLOAD_TOO_BIG = "单份不能超过 {limit} 字符"
UPLOAD_LIMIT = 100_000
"""单份上传的字符上限，与 ``MemoryWrite.content`` 的 max_length 保持一致。"""


class MemoryService:
    def __init__(self, session: AsyncSession, memory: AgentMemory) -> None:
        self.session = session
        self.memory = memory
        self.access = WorkspaceAccess(session)

    async def list_memories(self, user: User, workspace_id: str) -> list[str]:
        await self.access.permission(user, workspace_id)
        route = await self._route(workspace_id)
        paths = await self._call(self.memory.alist_memories, user.id, workspace_id)
        return [f"{route}{path}" for path in paths]

    async def read(self, user: User, workspace_id: str, path: str) -> tuple[str, str]:
        """读一份记忆，返回 ``(agent 眼里的路径, 内容)``；不存在 404。"""
        await self.access.permission(user, workspace_id)
        route = await self._route(workspace_id)
        relative = _relative(route, path)
        content = await self._call(
            self.memory.aread_memory, user.id, workspace_id, relative
        )
        if content is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=MEMORY_NOT_FOUND
            )
        return f"{route}{relative}", content

    async def write(
        self, user: User, workspace_id: str, path: str, content: str
    ) -> str:
        await self.access.require_writer(user, workspace_id)
        route = await self._route(workspace_id)
        stored = await self._call(
            self.memory.awrite_memory, user.id, workspace_id, _relative(route, path), content
        )
        return f"{route}{stored}"

    async def delete(self, user: User, workspace_id: str, path: str) -> None:
        await self.access.require_writer(user, workspace_id)
        route = await self._route(workspace_id)
        relative = _relative(route, path)
        if not await self._call(
            self.memory.adelete_memory, user.id, workspace_id, relative
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=MEMORY_NOT_FOUND
            )

    async def all(self, user: User) -> list[tuple[str, str, list[str]]]:
        """我全部空间下的记忆清单，返回 ``[(空间名, id, [路径, ...]), ...]``（含空的）。

        清单来自 ``WorkspaceService.visible``（成员关系 + 虚拟 default）—— 和 ``/memories/``
        挂载是同一份，所以在接口里看不到的空间，agent 那边也看不到。
        """
        visible = await WorkspaceService(self.session).visible(user)
        return [
            (name, workspace_id, await self.list_memories(user, workspace_id))
            for name, workspace_id in sorted(visible.items())
        ]

    async def upload(
        self, user: User, workspace_id: str, files: list[tuple[str, bytes]]
    ) -> list[tuple[str, str | None, str | None]]:
        """上传若干份文本文件，返回 ``[(文件名, 落库路径 | None, 失败原因 | None), ...]``。

        逐份处理：某一份坏（二进制 / 超大 / 脏文件名）只让那一份失败，其余照常落库 ——
        一次上传里藏着一份坏文件就让整批回滚，对用户没好处。覆盖语义与 ``write`` 一致。

        ``files`` 是 ``(filename, bytes)``：读 HTTP 请求体是路由层的活，校验和落库在这。
        """
        await self.access.require_writer(user, workspace_id)
        route = await self._route(workspace_id)
        results: list[tuple[str, str | None, str | None]] = []
        for filename, raw in files:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                results.append((filename, None, TEXT_ONLY))
                continue
            if len(text) > UPLOAD_LIMIT:
                results.append(
                    (filename, None, UPLOAD_TOO_BIG.format(limit=UPLOAD_LIMIT))
                )
                continue
            try:
                stored = await self.memory.awrite_memory(
                    user.id, workspace_id, filename, text
                )
            except ValueError as exc:  # 脏文件名（.. / ~ / 空）
                results.append((filename, None, str(exc)))
                continue
            results.append((filename, f"{route}{stored}", None))
        return results

    async def _route(self, workspace_id: str) -> str:
        """这个空间在 agent 眼里的格子前缀：``/memories/{空间名}/``。"""
        return f"{MEMORY_ROUTE}{await self.access.name_of(workspace_id)}/"

    @staticmethod
    async def _call(fn: Callable[..., Awaitable[Any]], *args: Any) -> Any:
        """调 AgentMemory：它用 ValueError 拒绝非法路径，这里换成 422。"""
        try:
            return await fn(*args)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc



def _relative(route: str, path: str) -> str:
    """把请求里的路径还原成格子内路径：``/memories/{空间名}/a.md`` -> ``a.md``。"""
    text = path.strip().replace("\\", "/")
    for prefix in (route, MEMORY_ROUTE):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    return text.lstrip("/")
