"""Deep Agents 异步封装。

持久化策略（对应 config.yaml 的 `postgresql.deepagent` 段）：

- **state / 短时记忆**：`AsyncPostgresSaver` 检查点，按 `thread_id` 落库，同一会话跨进程续聊；
- **store / 长期记忆**：`AsyncPostgresStore`，作为 `StoreBackend` 挂到 `/memories/` 路由下，
  跨会话、跨进程保留；其余路径走 `StateBackend`（线程内临时文件）。

用法::

    from agents import DeepAgent

    async with DeepAgent() as agent:
        answer = await agent.ainvoke("帮我记住代号是夜枭", thread_id="chat-1", user_id="u1")
        async for event in agent.astream("我刚说的代号是什么？", thread_id="chat-1", user_id="u1"):
            print(event.kind, event.text)

也可以用显式的 `aenter()` / `aexit()`（等价于 `async with`，便于在生命周期钩子里手动管理）。
"""

import asyncio
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Literal

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore
from pydantic import BaseModel

from config import DeepAgentConfig, PostgreConfig, app_config
from logger import logger

__all__ = ["MEMORY_ROUTE", "AgentContext", "AgentEvent", "DeepAgent"]

if sys.platform == "win32":
    # 与 dependencies/database.py 同样的兜底：单独跑脚本/测试时，psycopg 异步驱动
    # 在 ProactorEventLoop 上会直接报 InterfaceError（应用内由 uvicorn 的 loop 参数负责）。
    # ponytail: 事件循环策略 API 将在 Python 3.16 移除，届时改用 loop_factory。
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 持续化文件（长期记忆）挂在 store 上；其它路径仍在 state（线程内）
MEMORY_ROUTE = "/memories/"

DEFAULT_SYSTEM_PROMPT = """你是一个可长期协作的中文助手。

- 需要规划时先拆解任务再执行。
- 用户明确要求记住的内容，写入 `/memories/` 下的文件；该目录持久保存，重启后依然可读。
- 其它临时文件放普通路径即可，它们只在本会话内有效。
- 回答简洁、直接，不要复述这些规则。"""


class AgentContext(BaseModel):
    """运行期上下文：把用户 id 透传给 backend，用于 store 命名空间隔离。"""

    user_id: str = "default"


@dataclass(slots=True)
class AgentEvent:
    """`astream` 产出的事件。

    kind: ``token``（模型增量文本）、``tool_call``（工具调用名）、``done``（本轮结束，text 为完整回答）。
    """

    kind: Literal["token", "tool_call", "done"]
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class DeepAgent:
    """deepagents 的异步外壳：负责连接池、检查点、store 与图的生命周期。"""

    def __init__(
        self,
        *,
        config: DeepAgentConfig | None = None,
        postgres: PostgreConfig | None = None,
        tools: Sequence[BaseTool] | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        model: BaseChatModel | None = None,
    ) -> None:
        self.config = config or app_config.deepagent
        self.postgres = postgres or app_config.postgresql.deepagent
        self._tools = list(tools or [])
        self._system_prompt = system_prompt
        self._model = model
        self._stack: AsyncExitStack | None = None
        self._saver: AsyncPostgresSaver | None = None
        self._store: AsyncPostgresStore | None = None
        self._graph: Any = None

    # ---------- 生命周期 ----------

    async def __aenter__(self) -> "DeepAgent":
        """建立 saver/store 连接池、建表，并编译 deep agent 图。"""
        self._stack = AsyncExitStack()
        uri = self.postgres.uri
        try:
            self._saver = await self._stack.enter_async_context(
                AsyncPostgresSaver.from_conn_string(uri)
            )
            self._store = await self._stack.enter_async_context(
                AsyncPostgresStore.from_conn_string(uri)
            )
            # 幂等建表：checkpoints / checkpoint_blobs / checkpoint_writes / store
            await self._saver.setup()
            await self._store.setup()
            self._graph = self._build_graph()
        except BaseException:
            await self.__aexit__(*sys.exc_info())
            raise
        logger.info(
            "DeepAgent 就绪，模型 {}（checkpointer + store: {}@{}/{}）",
            self.config.model,
            self.postgres.user,
            self.postgres.host,
            self.postgres.db_name,
        )
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        """关闭连接池。幂等，可重复调用。"""
        stack, self._stack = self._stack, None
        self._saver = self._store = self._graph = None
        if stack is not None:
            await stack.aclose()

    async def aenter(self) -> "DeepAgent":
        """显式进入上下文（等价 `async with`），返回自身。"""
        return await self.__aenter__()

    async def aexit(self, *exc_info: Any) -> None:
        """显式退出上下文（等价 `async with` 退出）。"""
        await self.__aexit__(*exc_info)

    # ---------- 调用 ----------

    @property
    def graph(self) -> Any:
        """编译好的 LangGraph 图；未启动时抛错。"""
        if self._graph is None:
            raise RuntimeError("DeepAgent 尚未启动，请先 `async with DeepAgent() as agent:`")
        return self._graph

    @property
    def store(self) -> AsyncPostgresStore:
        """长期记忆（store）实例，可用于直接读写记忆。"""
        if self._store is None:
            raise RuntimeError("DeepAgent 尚未启动，请先 `async with DeepAgent() as agent:`")
        return self._store

    async def ainvoke(
        self,
        message: str | Sequence[BaseMessage],
        *,
        thread_id: str,
        user_id: str,
        recursion_limit: int = 100,
    ) -> str:
        """跑一轮对话并返回最终回答（state 自动落到 PostgreSQL 检查点）。"""
        state = await self.graph.ainvoke(
            self._input(message),
            config=self._run_config(thread_id, user_id, recursion_limit),
            context=AgentContext(user_id=user_id),
        )
        return _last_text(state.get("messages", []))

    async def astream(
        self,
        message: str | Sequence[BaseMessage],
        *,
        thread_id: str,
        user_id: str,
        recursion_limit: int = 100,
    ) -> AsyncIterator[AgentEvent]:
        """流式跑一轮对话：先逐个 token，最后补一个 kind="done" 的完整回答。"""
        async for mode, payload in self.graph.astream(
            self._input(message),
            config=self._run_config(thread_id, user_id, recursion_limit),
            context=AgentContext(user_id=user_id),
            stream_mode=["messages", "updates"],
        ):
            if mode == "messages":
                chunk = payload[0]
                if text := _chunk_text(chunk):
                    yield AgentEvent(kind="token", text=text)
                for call in getattr(chunk, "tool_call_chunks", None) or []:
                    if name := call.get("name"):
                        yield AgentEvent(kind="tool_call", text=name, data={"id": call.get("id")})
        # 图的最终回答从检查点里读，避免再解析一遍流式分片
        state = await self.aget_state(thread_id, user_id)
        yield AgentEvent(kind="done", text=state["answer"], data=state)

    async def aget_state(self, thread_id: str, user_id: str) -> dict[str, Any]:
        """读取某会话的短时记忆（检查点里的消息与最终回答）。"""
        snapshot = await self.graph.aget_state(self._run_config(thread_id, user_id))
        messages = (snapshot.values or {}).get("messages", [])
        return {
            "thread_id": thread_id,
            "user_id": user_id,
            "conversation_id": _conversation_id(thread_id, user_id),
            "messages": len(messages),
            "answer": _last_text(messages),
            "files": sorted((snapshot.values or {}).get("files", {}) or {}),
        }

    async def alist_memories(self, user_id: str, *, limit: int = 50) -> list[str]:
        """列出该用户长期记忆（store 命名空间）里保存的文件路径（含 /memories/ 前缀）。"""
        items = await self.store.asearch((user_id, "filesystem"), limit=limit)
        return sorted(f"{MEMORY_ROUTE}{item.key.lstrip('/')}" for item in items)

    # ---------- 内部 ----------

    def _build_graph(self) -> Any:
        store = self.store
        backend = CompositeBackend(
            default=StateBackend(),
            routes={
                # 每个用户一个命名空间：不同用户互相看不到对方的 /memories/ 文件。
                # deepagents 0.7 起 backend 只能传实例（工厂函数已移除），但 namespace
                # 仍支持按 Runtime 现算，所以按用户隔离依然成立。
                MEMORY_ROUTE: StoreBackend(
                    namespace=lambda rt: (rt.context.user_id, "filesystem"),
                    store=store,
                )
            },
        )

        return create_deep_agent(
            model=self._model or self._build_model(),
            tools=self._tools,
            system_prompt=self._system_prompt,
            backend=backend,
            checkpointer=self._saver,
            store=store,
            context_schema=AgentContext,
            name="deepagent",
        )

    def _build_model(self) -> BaseChatModel:
        return ChatOpenAI(
            model=self.config.model,
            base_url=self.config.base_url,
            api_key=self.config.api_key,
            temperature=self.config.temperature,
        )

    @staticmethod
    def _input(message: str | Sequence[BaseMessage]) -> dict[str, list[BaseMessage]]:
        messages = [HumanMessage(message)] if isinstance(message, str) else list(message)
        return {"messages": messages}

    @staticmethod
    def _run_config(thread_id: str, user_id: str, recursion_limit: int = 100) -> dict[str, Any]:
        # thread_id 带上用户前缀：猜不到别人的会话，也无法跨用户续聊
        return {
            "configurable": {"thread_id": _conversation_id(thread_id, user_id)},
            "recursion_limit": recursion_limit,
        }


def _conversation_id(thread_id: str, user_id: str) -> str:
    """检查点里的真实 thread_id：用户维度隔离。"""
    return f"{user_id}:{thread_id}"


def _chunk_text(chunk: BaseMessage) -> str:
    """流式分片里的文本（工具调用分片无文本，返回空串）。"""
    return chunk.text if isinstance(chunk.text, str) else ""


def _last_text(messages: Sequence[BaseMessage]) -> str:
    """最后一条有内容的 AI 消息。"""
    for message in reversed(messages):
        if message.__class__.__name__.startswith("AI") and (text := _chunk_text(message)):
            return text
    return ""
