"""最基础的聊天 Agent（简约版，替代 tmp.py 的复杂封装）。

持久化（连接参数见 agents/config.py，默认库 ``agents``）：

- **短期记忆**：``AsyncPostgresSaver`` 检查点，按 ``user_id:thread_id`` 落库，跨进程续聊；
- **长期记忆**：``AsyncPostgresStore`` + ``StoreBackend``，``/memories/`` 路径跨会话保留，
  命名空间 ``(user_id, workspace_id)``，等价于 store 里的 ``/memories/{user_id}/{workspace_id}/``；
  其余路径走 ``StateBackend``（线程内临时文件）。

记忆写入（两条路，互不干扰）：

- **agent 写**：调文件工具写 ``/memories/**`` 会 ``interrupt``，等人批准（见 ``MEMORY_PERMISSIONS``）；
- **用户写**：走 :class:`AgentMemory` 的 ``awrite_memory`` / ``adelete_memory``，不经模型、不需批准。

职责分离（对应 agents/readme.md）：

- :class:`GeneralAgent` 只管生命周期与聊天（``ainvoke`` / ``astream``）；
- :class:`AgentMemory` 只管记忆读写（``aget_state`` / ``alist_memories`` / 写入四件套），
  借用运行中 agent 的 graph、store 与 saver，自身无生命周期，由 ``agent.memory`` 暴露。

用法（FastAPI lifespan）::

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with GeneralAgent() as agent:
            app.state.agent = agent
            yield

    # 聊天：被拦下时 run.interrupt 非空，原样丢给用户看，批准后 resume 接着跑
    run = await agent.ainvoke("记住我喜欢简短回答", thread_id="t1", user_id="u1", workspace_id="ws1")
    if run.interrupt:
        run = await agent.ainvoke(
            thread_id="t1", user_id="u1", workspace_id="ws1",
            resume={"decisions": [{"type": "approve"}]},
        )
    print(run.answer)
    async for event in agent.astream("你好", thread_id="t1", user_id="u1", workspace_id="ws1"):
        print(event.kind, event.text, event.data)
    # 记忆：用户侧直接改（user_id / workspace_id 必须来自登录态，不能来自请求参数）
    memories = await agent.memory.alist_memories("u1", "ws1")
    await agent.memory.awrite_memory("u1", "ws1", "prefs.md", "喜欢简短回答")
"""

import asyncio
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Literal

from deepagents import FilesystemPermission, create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
from deepagents.backends.utils import create_file_data, validate_path
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, RemoveMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore
from langgraph.types import Command
from pydantic import BaseModel

from agents.config import ModelConfig, AgentPostgreConfig, model_cfg
from logger import logger

__all__ = [
    "MEMORY_PERMISSIONS",
    "MEMORY_ROUTE",
    "AgentContext",
    "AgentEvent",
    "AgentMemory",
    "AgentRun",
    "GeneralAgent",
]

if sys.platform == "win32":
    # 与 dependencies/database.py 同样的兜底：psycopg 异步驱动在 ProactorEventLoop 上
    # 会直接报 InterfaceError（应用内由 uvicorn 的 loop 参数负责）。
    # ponytail: 事件循环策略 API 将在 Python 3.16 移除，届时改用 loop_factory。
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 长期记忆（store）挂载的虚拟路径前缀；其余路径为线程内临时文件
MEMORY_ROUTE = "/memories/"

# agent（模型）对 /memories/** 的写入一律中断等人批准；用户侧写入走 AgentMemory，不经模型。
# 裸 "/memories"（无尾斜杠）匹配不上 "/memories/**"，单独列一条，否则能绕过规则写到路由根。
MEMORY_PERMISSIONS = [
    FilesystemPermission(
        operations=["write"],
        paths=[f"{MEMORY_ROUTE}**", MEMORY_ROUTE.rstrip("/")],
        mode="interrupt",
    )
]

DEFAULT_SYSTEM_PROMPT = """你是一个可长期协作的中文助手。

- 需要规划时先拆解任务再执行。
- 用户明确要求记住的内容，写入 `/memories/` 下的文件；该目录持久保存，重启后依然可读。
- 写 `/memories/` 会先请用户确认，确认后再落库。
- 其它临时文件放普通路径即可，它们只在本会话内有效。
- 回答简洁、直接，不要复述这些规则。"""


class AgentContext(BaseModel):
    """运行期上下文：透传给 StoreBackend，决定长期记忆落在哪个命名空间。

    ``(user_id, workspace_id)`` 等价于 store 里的 ``/memories/{user_id}/{workspace_id}/``。
    两个字段都没有默认值：漏传直接报错，而不是静默写进同一个共享命名空间。
    """

    user_id: str
    workspace_id: str


@dataclass(slots=True)
class AgentEvent:
    """`astream` 产出的事件。

    kind: ``token``（模型增量文本）、``tool_call``（工具调用名）、
    ``interrupt``（等人的批准，data 为待批请求）、``done``（本轮结束，text 为完整回答）。
    """

    kind: Literal["token", "tool_call", "interrupt", "done"]
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentRun:
    """`ainvoke` 的结果：正常跑完看 ``answer``，被拦下等人批准看 ``interrupt``。"""

    answer: str = ""
    interrupt: dict[str, Any] | None = None
    """待批准的请求（``action_requests`` / ``review_configs``），原样给前端展示。"""


class GeneralAgent:
    """聊天 Agent：只管连接池/图的生命周期与对话；记忆操作见 :class:`AgentMemory`。"""

    def __init__(
        self,
        *,
        config: ModelConfig | None = None,
        postgres: AgentPostgreConfig | None = None,
        tools: Sequence[BaseTool] | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        model: BaseChatModel | None = None,
    ) -> None:
        self.config = config or model_cfg
        self.postgres = postgres or AgentPostgreConfig()
        self._tools = list(tools or [])
        self._system_prompt = system_prompt
        self._model = model
        self._stack: AsyncExitStack | None = None
        self._graph: Any = None
        # 启动后可用：记忆操作入口（借 graph/store，无独立生命周期）
        self.memory: AgentMemory | None = None

    # ---------- 生命周期 ----------

    async def __aenter__(self) -> "GeneralAgent":
        """建 saver/store 连接池、幂等建表、编译图；中途失败自动回滚已建连接。"""
        async with AsyncExitStack() as stack:
            saver = await stack.enter_async_context(
                AsyncPostgresSaver.from_conn_string(self.postgres.uri)
            )
            store = await stack.enter_async_context(
                AsyncPostgresStore.from_conn_string(self.postgres.uri)
            )
            await saver.setup()
            await store.setup()
            graph = create_deep_agent(
                model=self._model or self._build_model(),
                tools=self._tools,
                system_prompt=self._system_prompt,
                backend=CompositeBackend(
                    default=StateBackend(),
                    routes={
                        # 一个 (用户, 空间) 一个命名空间，互相看不到对方的 /memories/ 文件
                        MEMORY_ROUTE: StoreBackend(
                            namespace=lambda rt: (
                                rt.context.user_id,
                                rt.context.workspace_id,
                                "filesystem",
                            ),
                            store=store,
                        )
                    },
                ),
                permissions=MEMORY_PERMISSIONS,
                checkpointer=saver,
                store=store,
                context_schema=AgentContext,
                name="general-agent",
            )
            self._stack = stack.pop_all()  # 所有权移交给 self；异常时上面已自动清理
            self._graph = graph
            self.memory = AgentMemory(graph, store, saver)
        logger.info(
            "GeneralAgent 就绪，模型 {}（checkpointer + store: {}@{}/{}）",
            self.config.model,  # pyright: ignore[reportOptionalMemberAccess]
            self.postgres.user,
            self.postgres.host,
            self.postgres.db_name,
        )
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        """关闭连接池。幂等，可重复调用。"""
        self.memory = None
        self._graph = None
        stack, self._stack = self._stack, None
        if stack is not None:
            await stack.aclose()

    # ---------- 聊天 ----------

    async def ainvoke(
        self,
        message: str | Sequence[BaseMessage] | None = None,
        *,
        thread_id: str,
        user_id: str,
        workspace_id: str,
        recursion_limit: int = 100,
        resume: dict[str, Any] | None = None,
    ) -> AgentRun:
        """跑一轮对话（短期记忆自动落检查点）。

        ``resume`` 是上一次 ``run.interrupt`` 的答复（``{"decisions": [...]}``），
        传了它就不需要 ``message``，图从中断点接着跑。
        """
        state = await self._require_graph().ainvoke(
            _payload(message, resume),
            config=_run_config(thread_id, user_id, recursion_limit, workspace_id),
            context=AgentContext(user_id=user_id, workspace_id=workspace_id),
        )
        return AgentRun(answer=_last_text(state.get("messages", [])), interrupt=_pending(state))

    async def astream(
        self,
        message: str | Sequence[BaseMessage] | None = None,
        *,
        thread_id: str,
        user_id: str,
        workspace_id: str,
        recursion_limit: int = 100,
        resume: dict[str, Any] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """流式跑一轮：逐 token / 工具调用事件，需要批准时补一个 interrupt，最后补 done。"""
        pending: dict[str, Any] | None = None
        final: dict[str, Any] = {}
        async for mode, payload in self._require_graph().astream(
            _payload(message, resume),
            config=_run_config(thread_id, user_id, recursion_limit, workspace_id),
            context=AgentContext(user_id=user_id, workspace_id=workspace_id),
            stream_mode=["messages", "values"],
        ):
            if mode == "messages":
                chunk = payload[0]
                if text := _chunk_text(chunk):
                    yield AgentEvent(kind="token", text=text)
                for call in getattr(chunk, "tool_call_chunks", None) or []:
                    if name := call.get("name"):
                        yield AgentEvent(kind="tool_call", text=name, data={"id": call.get("id")})
            else:  # values：每个超步后的完整 state，留最后一份取完整回答
                final = payload
                pending = _pending(payload) or pending
        if pending is not None:
            yield AgentEvent(kind="interrupt", data=pending)
        yield AgentEvent(kind="done", text=_last_text(final.get("messages", [])))

    # ---------- 内部 ----------

    def _require_graph(self) -> Any:
        """未启动时给出明确报错（替代 tmp.py 里成堆的 @property 判空）。"""
        if self._graph is None:
            raise RuntimeError("GeneralAgent 尚未启动，请先 `async with GeneralAgent() as agent:`")
        return self._graph

    def _build_model(self) -> BaseChatModel:
        if not self.config.ready:
            raise RuntimeError(
                "未读到模型配置，请设置环境变量 OPEN_MODEL / OPEN_BASE_URL / OPEN_API_KEY（或写进项目根目录 .env）"
            )
        return ChatOpenAI(
            model=self.config.model,
            base_url=self.config.base_url,
            api_key=self.config.api_key,
        )


class AgentMemory:
    """记忆读写：借用运行中 :class:`GeneralAgent` 的 graph / store / saver，自身无生命周期。

    由 ``GeneralAgent.__aenter__`` 创建（``agent.memory``），agent 关闭即失效。
    写入是用户侧直写 store：不经模型、不触发人工批准；agent 侧写入走文件工具，受
    ``MEMORY_PERMISSIONS`` 的 interrupt 约束。
    """

    def __init__(
        self, graph: Any, store: AsyncPostgresStore, saver: AsyncPostgresSaver
    ) -> None:
        self._graph = graph
        self._store = store
        self._saver = saver

    async def aget_state(self, thread_id: str, user_id: str) -> dict[str, Any]:
        """短期记忆：该会话检查点里的消息数、最终回答与临时文件。"""
        snapshot = await self._graph.aget_state(_run_config(thread_id, user_id))
        values = snapshot.values or {}
        messages = values.get("messages", [])
        return {
            "thread_id": thread_id,
            "user_id": user_id,
            "conversation_id": _conversation_id(thread_id, user_id),
            "messages": len(messages),
            "answer": _last_text(messages),
            "files": sorted(values.get("files", {}) or {}),
        }

    async def aget_meta(self, thread_id: str, user_id: str) -> dict[str, Any] | None:
        """会话归属：checkpoint metadata（没有这个会话就返回 ``None``）。

        值来自 ``_run_config`` 里的 metadata，所以路由可以用它确认「这是不是我的会话」
        并取回当时的 workspace_id，不必另建归属表。
        """
        snapshot = await self._graph.aget_state(_run_config(thread_id, user_id))
        return dict(snapshot.metadata) if snapshot.metadata else None

    async def alist_threads(
        self, user_id: str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        """我自己的会话列表（来自 checkpoint metadata，不另建表）。

        checkpointer 的 ``alist(filter=...)`` 按 metadata 过滤且新→旧，但一个会话有多个
        超步就多条记录，所以这里按 thread_id 去重、保留每条最新的一份
        （``limit`` 是 checkpoint 条数上限，不是会话数）。
        """
        threads: dict[str, dict[str, Any]] = {}
        async for item in self._saver.alist(
            None, filter={"user_id": user_id}, limit=limit
        ):
            conversation_id = item.config["configurable"]["thread_id"]
            threads.setdefault(
                conversation_id,
                {
                    # 对外只暴露不带用户前缀的短 id
                    "thread_id": _short_thread_id(conversation_id, user_id),
                    "workspace_id": item.metadata.get("workspace_id"),
                    "updated_at": item.checkpoint.get("ts"),
                },
            )
        return list(threads.values())

    async def alist_messages(
        self, thread_id: str, user_id: str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        """短期记忆：该会话检查点里的消息列表（最旧→最新，取最后 ``limit`` 条）。

        ``messages`` 是 ``add_messages`` reducer，每条消息带稳定 ``id``，删单条就用它。
        """
        snapshot = await self._graph.aget_state(_run_config(thread_id, user_id))
        messages: Sequence[BaseMessage] = (snapshot.values or {}).get("messages", [])
        return [
            {
                "id": message.id,
                "role": message.type,
                "content": message.text if isinstance(message.text, str) else "",
            }
            for message in messages[-limit:]
        ]

    async def aedit_state(
        self,
        thread_id: str,
        user_id: str,
        *,
        delete_messages: Sequence[str] = (),
        delete_files: Sequence[str] = (),
    ) -> None:
        """编辑短期记忆：删消息 / 删会话内临时文件（都基于最新检查点写一份新状态）。

        两个 reducer 的语义不一样，别搞反：

        - ``messages`` 是 ``add_messages``：直接传列表是**追加**，删必须用 ``RemoveMessage``；
        - ``files`` 是 dict reducer：key 给 ``None`` 才是删。

        改完之后再聊就是接在修改后的历史上（已实测）。
        """
        state: dict[str, Any] = {}
        if delete_messages:
            state["messages"] = [RemoveMessage(id=mid) for mid in delete_messages]
        if delete_files:
            state["files"] = {path: None for path in delete_files}
        if not state:
            return
        # update_state 会写一份新检查点，metadata 得把 workspace_id 一起带上，
        # 否则会话归属（/chat/state、/chat/mine）就读不到了
        snapshot = await self._graph.aget_state(_run_config(thread_id, user_id))
        config = _run_config(
            thread_id,
            user_id,
            workspace_id=(snapshot.metadata or {}).get("workspace_id"),
        )
        await self._graph.aupdate_state(config, state)

    async def adelete_thread(self, thread_id: str, user_id: str) -> None:
        """删整条会话（短期记忆）。

        只删检查点，**不动长期记忆**（``/memories/`` 是另一个生命周期，得显式删）。
        """
        await self._saver.adelete_thread(_conversation_id(thread_id, user_id))

    async def alist_memories(
        self, user_id: str, workspace_id: str, *, limit: int = 50
    ) -> list[str]:
        """长期记忆：列出该 (用户, 空间) 下 /memories/ 里保存的文件路径。

        store 里的 key 不含 /memories/ 前缀（CompositeBackend 路由时剥掉了），这里补回。
        """
        items = await self._store.asearch(_memory_ns(user_id, workspace_id), limit=limit)
        return sorted(f"{MEMORY_ROUTE}{item.key.lstrip('/')}" for item in items)

    async def aread_memory(self, user_id: str, workspace_id: str, path: str) -> str | None:
        """读一份长期记忆，不存在返回 ``None``（``path`` 可写 ``prefs.md`` 或 ``/memories/prefs.md``）。"""
        item = await self._store.aget(_memory_ns(user_id, workspace_id), _memory_key(path))
        content = item.value.get("content") if item else None
        return content if isinstance(content, str) else None

    async def awrite_memory(
        self, user_id: str, workspace_id: str, path: str, content: str
    ) -> str:
        """写/覆盖一份长期记忆，返回它的虚拟路径（格式与工具写的一致，agent 能直接读到）。"""
        key = _memory_key(path)
        await self._store.aput(
            _memory_ns(user_id, workspace_id), key, create_file_data(content)
        )
        return f"{MEMORY_ROUTE}{key.lstrip('/')}"

    async def adelete_memory(self, user_id: str, workspace_id: str, path: str) -> bool:
        """删一份长期记忆，不存在返回 ``False``。"""
        namespace = _memory_ns(user_id, workspace_id)
        key = _memory_key(path)
        if await self._store.aget(namespace, key) is None:
            return False
        await self._store.adelete(namespace, key)
        return True


# ---------- 模块级小工具（聊天与记忆共用） ----------


def _conversation_id(thread_id: str, user_id: str) -> str:
    """检查点里的真实 thread_id：加用户前缀，实现用户维度隔离。"""
    return f"{user_id}:{thread_id}"


def _short_thread_id(conversation_id: str, user_id: str) -> str:
    """把检查点里的 thread_id 还原成对外暴露的短 id（去掉用户前缀）。"""
    return conversation_id.removeprefix(f"{user_id}:")


def _memory_ns(user_id: str, workspace_id: str) -> tuple[str, str, str]:
    """长期记忆的 store 命名空间：一个 (用户, 空间) 一份，互相看不见。"""
    return (user_id, workspace_id, "filesystem")


def _memory_key(path: str) -> str:
    """用户侧传入的记忆路径 -> store key（与工具写 ``/memories/x`` 落库的 key 同形：``/x``）。"""
    normalized = validate_path(f"{MEMORY_ROUTE}{path.strip().removeprefix(MEMORY_ROUTE).lstrip('/')}")
    key = normalized[len(MEMORY_ROUTE) - 1 :]
    if not key.strip("/"):
        raise ValueError(f"记忆路径不能为空：{path!r}")
    return key


def _pending(state: dict[str, Any]) -> dict[str, Any] | None:
    """从 state 里取待人工批准的请求（``Interrupt.value``，即 HITLRequest）。"""
    items = state.get("__interrupt__") or []
    return items[0].value if items else None


def _payload(
    message: str | Sequence[BaseMessage] | None, resume: dict[str, Any] | None
) -> Any:
    """首轮用 message 起跑；带 ``resume`` 时从中断点接着跑（此时不需要 message）。"""
    return Command(resume=resume) if resume is not None else _input(message)


def _run_config(
    thread_id: str,
    user_id: str,
    recursion_limit: int = 100,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    """运行配置。

    ``metadata`` 会被 langgraph 的 ``get_checkpoint_metadata`` 合并进 ``checkpoints.metadata``，
    所以「这个会话属于谁 / 属于哪个空间」不用另建表：写一次就存在检查点里，
    读用 ``aget_state().metadata``，列用 ``saver.alist(filter={"user_id": ...})``。
    """
    metadata: dict[str, Any] = {"user_id": user_id}
    if workspace_id is not None:
        metadata["workspace_id"] = workspace_id
    return {
        "configurable": {"thread_id": _conversation_id(thread_id, user_id)},
        "recursion_limit": recursion_limit,
        "metadata": metadata,
    }


def _input(message: str | Sequence[BaseMessage] | None) -> dict[str, list[BaseMessage]]:
    if message is None:
        raise ValueError("首轮对话要传 message；只有 resume 时才可以省略")
    messages = [HumanMessage(message)] if isinstance(message, str) else list(message)
    return {"messages": messages}  # pyright: ignore[reportReturnType]


def _chunk_text(chunk: BaseMessage) -> str:
    """流式分片里的文本（工具调用分片无文本，返回空串）。"""
    return chunk.text if isinstance(chunk.text, str) else ""


def _last_text(messages: Sequence[BaseMessage]) -> str:
    """最后一条有内容的 AI 消息。"""
    for message in reversed(messages):
        if isinstance(message, AIMessage) and (text := _chunk_text(message)):
            return text
    return ""
