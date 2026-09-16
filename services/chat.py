"""聊天服务：会话归属校验 + 调 ``GeneralAgent`` 跑一轮。

鉴权链条（每一步都不看请求体里的 user_id）：

1. ``user_id`` 来自登录态；
2. 真正落库的 ``conversation_id`` 是 ``{user_id}:{thread_id}``（agent 内部拼），
   所以前端猜别人的 thread_id 也读不到任何东西；
3. 归属校验读 checkpoint metadata（``AgentMemory.aget_meta``），查不到一律 404；
4. ``workspace_id`` 从 metadata 取（不信任请求体），每轮再校验一次成员权限 ——
   被移出空间后立刻失效。
"""

from collections.abc import AsyncIterator
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from agents.agent import AgentEvent, AgentMemory, AgentRun, GeneralAgent
from models import User
from services.access import WorkspaceAccess

__all__ = ["THREAD_NOT_FOUND", "ChatService"]

THREAD_NOT_FOUND = "会话不存在"
AGENT_NOT_READY = "Agent 未就绪（未配置模型或服务正在启动）"


class ChatService:
    def __init__(self, session: AsyncSession, agent: GeneralAgent) -> None:
        memory: AgentMemory | None = agent.memory
        if memory is None:  # AgentDep 只返回启动好的 agent，这里兜底
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=AGENT_NOT_READY
            )
        self.agent = agent
        self.memory = memory
        self.access = WorkspaceAccess(session)

    async def open_turn(
        self, user: User, *, workspace_id: str, thread_id: str | None = None
    ) -> str:
        """开一轮对话前的把关：有空间权限（viewer 也能聊）并定下 thread_id。"""
        await self.access.permission(user, workspace_id)
        return thread_id or uuid4().hex

    async def send(
        self,
        user: User,
        *,
        workspace_id: str,
        message: str,
        thread_id: str | None = None,
    ) -> tuple[str, AgentRun]:
        conversation = await self.open_turn(
            user, workspace_id=workspace_id, thread_id=thread_id
        )
        run = await self.agent.ainvoke(
            message, thread_id=conversation, user_id=user.id, workspace_id=workspace_id
        )
        return conversation, run

    async def stream(
        self,
        user: User,
        *,
        workspace_id: str,
        message: str,
        thread_id: str | None = None,
    ) -> tuple[str, AsyncIterator[AgentEvent]]:
        conversation = await self.open_turn(
            user, workspace_id=workspace_id, thread_id=thread_id
        )
        events = self.agent.astream(
            message, thread_id=conversation, user_id=user.id, workspace_id=workspace_id
        )
        return conversation, events

    async def approve(
        self, user: User, *, thread_id: str, decisions: list[dict]
    ) -> AgentRun:
        """人工批准后接着跑：空间取自会话 metadata，不接受请求体里的 workspace_id。"""
        workspace_id = await self.own(user, thread_id)
        return await self.agent.ainvoke(
            thread_id=thread_id,
            user_id=user.id,
            workspace_id=workspace_id,
            resume={"decisions": decisions},
        )

    async def state(self, user: User, thread_id: str) -> dict:
        await self.own(user, thread_id)
        return await self.memory.aget_state(thread_id, user.id)

    async def list_threads(self, user: User, *, limit: int = 200) -> list[dict]:
        return await self.memory.alist_threads(user.id, limit=limit)

    async def history(
        self, user: User, thread_id: str, *, limit: int = 200
    ) -> list[dict]:
        await self.own(user, thread_id)
        return await self.memory.alist_messages(thread_id, user.id, limit=limit)

    async def delete_messages(
        self, user: User, *, thread_id: str, message_ids: list[str]
    ) -> None:
        await self.own(user, thread_id)
        await self.memory.aedit_state(
            thread_id, user.id, delete_messages=message_ids
        )

    async def delete_files(
        self, user: User, *, thread_id: str, paths: list[str]
    ) -> None:
        await self.own(user, thread_id)
        await self.memory.aedit_state(thread_id, user.id, delete_files=paths)

    async def delete_thread(self, user: User, thread_id: str) -> None:
        await self.own(user, thread_id)
        await self.memory.adelete_thread(thread_id, user.id)

    async def own(self, user: User, thread_id: str) -> str:
        """确认这条 thread_id 是本人的会话，返回它绑定的 workspace_id。"""
        meta = await self.memory.aget_meta(thread_id, user.id)
        if meta is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=THREAD_NOT_FOUND
            )
        workspace_id = meta.get("workspace_id")
        await self.access.permission(user, workspace_id)  # 还在空间里才行
        return workspace_id
