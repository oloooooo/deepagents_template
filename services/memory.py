"""长期记忆服务：调 ``AgentMemory`` 读写 store。

规则（与 ``agents/readme.md`` 一致）：

- 记忆库按 ``user_id`` 隔离，``user_id`` 只能来自登录态；
- 路径校验的 ValueError 一律转 422，别让它冒成 500。

**路径在响应里是 agent 眼里的写法**（``/memories/notes/a.md``）：REST 按 id 寻址、
agent 按路径寻址，两边在响应里对齐，用户拿到的路径可以原样丢给 agent（``docs/adr/0009``）。
请求里的路径两种写法都收：``notes/a.md`` / ``/memories/notes/a.md``。
"""

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException, status

from agents.agent import MEMORY_ROUTE, AgentMemory
from models import User

__all__ = ["MEMORY_NOT_FOUND", "MemoryService"]

MEMORY_NOT_FOUND = "记忆不存在"
TEXT_ONLY = "只收 UTF-8 文本文件（二进制请另行存放）"
UPLOAD_TOO_BIG = "单份不能超过 {limit} 字符"
UPLOAD_LIMIT = 100_000
"""单份上传的字符上限，与 ``MemoryWrite.content`` 的 max_length 保持一致。"""

ROUTE = MEMORY_ROUTE
"""响应里的记忆路径前缀：``/memories/``。"""


class MemoryService:
    def __init__(self, memory: AgentMemory) -> None:
        self.memory = memory

    async def list_memories(self, user: User) -> list[str]:
        paths = await self._call(self.memory.alist_memories, user.id)
        return [f"{ROUTE}{path}" for path in paths]

    async def read(self, user: User, path: str) -> tuple[str, str]:
        """读一份记忆，返回 ``(agent 眼里的路径, 内容)``；不存在 404。"""
        relative = _relative(path)
        content = await self._call(self.memory.aread_memory, user.id, relative)
        if content is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=MEMORY_NOT_FOUND
            )
        return f"{ROUTE}{relative}", content

    async def write(self, user: User, path: str, content: str) -> str:
        stored = await self._call(
            self.memory.awrite_memory, user.id, _relative(path), content
        )
        return f"{ROUTE}{stored}"

    async def delete(self, user: User, path: str) -> None:
        relative = _relative(path)
        if not await self._call(self.memory.adelete_memory, user.id, relative):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=MEMORY_NOT_FOUND
            )

    async def upload(
        self, user: User, files: list[tuple[str, bytes]]
    ) -> list[tuple[str, str | None, str | None]]:
        """上传若干份文本文件，返回 ``[(文件名, 落库路径 | None, 失败原因 | None), ...]``。

        逐份处理：某一份坏（二进制 / 超大 / 脏文件名）只让那一份失败，其余照常落库 ——
        一次上传里藏着一份坏文件就让整批回滚，对用户没好处。覆盖语义与 ``write`` 一致。

        ``files`` 是 ``(filename, bytes)``：读 HTTP 请求体是路由层的活，校验和落库在这。
        """
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
                stored = await self.memory.awrite_memory(user.id, filename, text)
            except ValueError as exc:  # 脏文件名（.. / ~ / 空）
                results.append((filename, None, str(exc)))
                continue
            results.append((filename, f"{ROUTE}{stored}", None))
        return results

    @staticmethod
    async def _call(fn: Callable[..., Awaitable[Any]], *args: Any) -> Any:
        """调 AgentMemory：它用 ValueError 拒绝非法路径，这里换成 422。"""
        try:
            return await fn(*args)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc


def _relative(path: str) -> str:
    """把请求里的路径还原成记忆内路径：``/memories/a.md`` -> ``a.md``。"""
    text = path.strip().replace("\\", "/")
    if text.startswith(ROUTE):
        text = text[len(ROUTE) :]
    return text.lstrip("/")
