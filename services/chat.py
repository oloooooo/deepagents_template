"""聊天服务：会话归属校验 + 调 ``GeneralAgent`` 跑一轮。

鉴权链条（每一步都不看请求体里的 user_id）：

1. ``user_id`` 来自登录态；
2. 真正落库的 ``conversation_id`` 是 ``{user_id}:{thread_id}``（agent 内部拼），
   所以前端猜别人的 thread_id 也读不到任何东西；
3. 归属校验读 checkpoint metadata（``AgentMemory.aget_meta``），查不到一律 404；
4. ``workspace_id`` 从 metadata 取（不信任请求体），每轮再校验一次成员权限 ——
   被移出空间后立刻失效；metadata 里没有它（虚拟 default 空间之前建的会话）就当 ``default``。

``public_workspaces``（可见的公共空间，名字 -> id）是**每轮由路由传进来**的，不落 metadata：
被移出公共空间必须立刻失效，包括 ``/chat/approve`` 续跑那一轮（见 ``docs/adr/0004``）。

停止（用户按暂停键）与中断（agent 等人批准）是两件事，别搞反（见 ``CONTEXT.md``）：

- **中断**是 agent 的动作，可续跑，走 ``/chat/approve``；
- **停止**是用户的动作，不可续跑，走 ``/chat/stop``：取消在跑的那一轮，然后把检查点收尾。

一个会话同时只允许一轮在跑（:meth:`ChatService.open_turn` 里同步占位，重复就 409），
所以“停哪一轮”永远无歧义。注册表在 ``agent.turns`` 上，**只在单进程有效**（``docs/adr/0006``）。
"""

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from agents.agent import AgentEvent, AgentMemory, AgentRun, GeneralAgent, Turn
from logger import logger
from models import DEFAULT_WORKSPACE, User
from services.access import WorkspaceAccess

__all__ = ["THREAD_NOT_FOUND", "TURN_RUNNING", "ChatService"]

THREAD_NOT_FOUND = "会话不存在"
AGENT_NOT_READY = "Agent 未就绪（未配置模型或服务正在启动）"
TURN_RUNNING = "这个会话还有一轮在跑"
STOP_TIMEOUT = 5.0
"""等被取消的那一轮真正停下来，最多等这么久（秒）。"""


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
    ) -> tuple[str, Turn]:
        """开一轮对话前的把关：没在跑别的轮次、有空间权限（viewer 也能聊），并定下 thread_id。

        **占位必须发生在任何 ``await`` 之前**：否则两个并发请求会双双通过检查，同一个会话
        上跑起两轮（langgraph 对同一 thread 的并发写会互相覆盖）。占位成功但鉴权失败时
        要释放，否则一条无权限的请求就能把会话永久锁住。
        """
        conversation = thread_id or uuid4().hex
        turn = self.agent.turns.reserve(conversation)
        if turn is None:
            raise HTTPException(status.HTTP_409_CONFLICT, TURN_RUNNING)
        try:
            await self.access.permission(user, workspace_id)
        except Exception:
            self.agent.turns.release(turn)
            raise
        return conversation, turn

    async def send(
        self,
        user: User,
        *,
        workspace_id: str,
        message: str,
        public_workspaces: dict[str, str],
        thread_id: str | None = None,
    ) -> tuple[str, AgentRun]:
        conversation, turn = await self.open_turn(
            user, workspace_id=workspace_id, thread_id=thread_id
        )
        try:
            run = await self.agent.ainvoke(
                message,
                thread_id=conversation,
                user_id=user.id,
                workspace_id=workspace_id,
                public_workspaces=public_workspaces,
            )
        finally:
            self.agent.turns.release(turn)
        return conversation, run

    async def stream(
        self,
        user: User,
        *,
        workspace_id: str,
        message: str,
        public_workspaces: dict[str, str],
        thread_id: str | None = None,
    ) -> tuple[str, AsyncIterator[AgentEvent]]:
        conversation, turn = await self.open_turn(
            user, workspace_id=workspace_id, thread_id=thread_id
        )
        events = self.agent.astream(
            message,
            thread_id=conversation,
            user_id=user.id,
            workspace_id=workspace_id,
            public_workspaces=public_workspaces,
        )
        return conversation, self._tracked(events, turn)

    async def _tracked(
        self, events: AsyncIterator[AgentEvent], turn: Turn
    ) -> AsyncIterator[AgentEvent]:
        """给事件流挂上“这一轮在跑”的生命周期，并攒下已流出的文本。

        攒文本是必须的：AI 消息只在超步结束时落检查点，所以被停止时已经流出去的增量在
        历史里根本不存在，只能实时记着，停止时再写回去。

        收到 ``tool_call`` 就把攒的清零 —— 只保留“正在生成的那一段”，否则一轮里多次
        工具往返会把几段回答粘成一条。

        顺便把 ``turn.task`` 换成**当前这个子任务**：Starlette 的 ``StreamingResponse``
        把迭代放在一个子任务里跑，而 ``open_turn`` 当时拿到的是整个请求任务。
        取消子任务只会让这条流结束；取消请求任务则是把整个 HTTP 请求连根拔掉。
        """
        turn.task = asyncio.current_task()
        try:
            async for event in events:
                if event.kind == "token":
                    turn.text += event.text
                elif event.kind == "tool_call":
                    turn.text = ""
                yield event
        finally:
            self.agent.turns.release(turn)

    async def stop(self, user: User, *, thread_id: str) -> str:
        """用户按下停止：取消在跑的那一轮，并把短期记忆收尾。

        返回这一轮最终留在历史里的文本。三种情况走同一条路：

        - 正在跑 → 取消它，再把已流出的部分文本写回历史；
        - 正等人批准（中断态）→ 没有在跑的任务，直接收尾（丢弃待批准的请求）；
        - 已经跑完 → 什么都不做，返回原来的回答（幂等，重复按不会污染历史）。

        归属走 ``own``：别人的 / 不存在的会话一律 404，不泄露存在性。
        """
        workspace_id = await self.own(user, thread_id)
        turn = self.agent.turns.get(thread_id)
        if turn is not None:
            await self._cancel(turn)
        return await self.memory.astop(
            thread_id, user.id, workspace_id, text=turn.text if turn else ""
        )

    async def _cancel(self, turn: Turn) -> None:
        """取消在跑的那一轮，并等它真的停下来。

        ``asyncio.wait`` 而不是 ``await task``：被取消的 task 会抛 ``CancelledError``，
        ``wait`` 不把它传播出来。

        超时兜底：工具如果卡在不可取消的阻塞调用里（比如 ``run_in_executor`` 里的同步 IO），
        ``cancel()`` 不会立刻生效。这时不能把停止按钮变成转圈 —— 先返回，收尾照做。
        """
        task = turn.task
        if task is None or task.done():
            return
        task.cancel()
        _, pending = await asyncio.wait({task}, timeout=STOP_TIMEOUT)
        if pending:
            # 那一轮还活着，稍后可能再写一份检查点、盖掉我们的收尾；概率低但没法根除
            logger.warning(
                "停止轮次超时（{}s），不等待收尾：thread_id={}", STOP_TIMEOUT, turn.thread_id
            )

    async def approve(
        self,
        user: User,
        *,
        thread_id: str,
        decisions: list[dict],
        public_workspaces: dict[str, str],
    ) -> AgentRun:
        """人工批准后接着跑：空间取自会话 metadata，不接受请求体里的 workspace_id。

        公共空间可见范围**重新传一遍**（不取 metadata）：续跑也要反映最新的授权状态。
        """
        workspace_id = await self.own(user, thread_id)
        turn = self.agent.turns.reserve(thread_id)
        if turn is None:
            raise HTTPException(status.HTTP_409_CONFLICT, TURN_RUNNING)
        try:
            return await self.agent.ainvoke(
                thread_id=thread_id,
                user_id=user.id,
                workspace_id=workspace_id,
                public_workspaces=public_workspaces,
                resume={"decisions": decisions},
            )
        finally:
            self.agent.turns.release(turn)

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
        # 虚拟 default 空间之前建的会话 metadata 里没有 workspace_id，一律按 default 处理
        workspace_id = meta.get("workspace_id") or DEFAULT_WORKSPACE
        await self.access.permission(user, workspace_id)  # 还在空间里才行
        return workspace_id
