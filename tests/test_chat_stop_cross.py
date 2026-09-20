"""验证跨进程停止：停止请求落到**没有**那一轮的进程上，仍然能停掉并拿到结果。

两个 ``GeneralAgent`` 实例 = 两个 worker（各自的 registry、各自的 LISTEN 连接、各自的表读），
共用同一个 Postgres。这是对多进程最接近的模拟，不用真开两个进程。

运行方式：``uv run python tests/test_chat_stop_cross.py``（自建自清）

覆盖：跨进程停止并回传答案、跨进程 409、没人在跑时直接收尾、心跳回收陈行、孤儿检查点恢复。
"""

import asyncio
import sys
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402
from langchain_core.language_models import BaseChatModel  # noqa: E402
from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    FakeMessagesListChatModel,
)
from langchain_core.messages import (  # noqa: E402
    AIMessage,
    AIMessageChunk,
    HumanMessage,
)
from langchain_core.outputs import (  # noqa: E402
    ChatGeneration,
    ChatGenerationChunk,
    ChatResult,
)

from agents.agent import GeneralAgent  # noqa: E402
from agents.config import AgentPostgreConfig  # noqa: E402
from agents.turns import CHANNEL, TURN_HEARTBEAT_TTL  # noqa: E402
from models import User  # noqa: E402
from services.chat import ChatService  # noqa: E402

SUFFIX = uuid4().hex[:8]
USER = f"u_cross_{SUFFIX}"
WORKSPACE = "default"  # 虚拟空间，WorkspaceAccess 不查库，所以 session 可以给 None

checks = 0


def step(name: str) -> None:
    global checks
    checks += 1
    print(f"  [{checks:02d}] {name}")


class ToolModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return self


class SlowModel(BaseChatModel):
    """一次吐一堆 chunk 的假模型，好在中途掐掉（真·流式，不是一次性返回）。"""

    CHUNKS: ClassVar[str] = "一二三四五六七八九十甲乙丙丁"
    DELAY: ClassVar[float] = 0.05

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return self

    @property
    def _llm_type(self) -> str:
        return "slow-stream"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return ChatResult(generations=[ChatGeneration(message=AIMessage("完整回答"))])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001, ANN003, ANN201
        for char in self.CHUNKS:
            await asyncio.sleep(self.DELAY)
            yield ChatGenerationChunk(message=AIMessageChunk(content=char))


def agent_db_execute(sql: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(AgentPostgreConfig().uri, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def cleanup() -> None:
    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        agent_db_execute(f"delete from {table} where thread_id like %s", (f"{USER}:%",))
    agent_db_execute("delete from store where prefix like %s", (f"{USER}.%",))
    agent_db_execute("delete from running_turns where thread_id like %s", (f"%{SUFFIX}%",))


async def history(agent: GeneralAgent, thread_id: str) -> list[tuple[str, str]]:
    assert agent.memory is not None
    return [
        (m["role"], m["content"])
        for m in await agent.memory.alist_messages(thread_id, USER)
    ]


async def start_turn(agent: GeneralAgent, thread_id: str, message: str) -> asyncio.Task:
    """在 ``agent`` 上开一轮（模拟"这一轮跑在这台 worker 上"），返回它的 task。"""
    assert agent.running_turns is not None
    assert await agent.running_turns.open_turn(thread_id), "拿不到轮次登记"
    turn = agent.turns.reserve(thread_id, USER, WORKSPACE)

    async def consume() -> None:
        rows = agent.running_turns  # 捕获下来：__aexit__ 之后 self.running_turns 会是 None
        turn.task = asyncio.current_task()  # 和 ChatService._tracked 做的事一样
        try:
            async for event in agent.astream(
                message, thread_id=thread_id, user_id=USER, workspace_id=WORKSPACE
            ):
                if event.kind == "token":
                    turn.text += event.text
                elif event.kind == "tool_call":
                    turn.text = ""
        finally:
            agent.turns.release(turn)
            if rows is not None:
                await rows.close_turn(thread_id)

    return asyncio.create_task(consume())


async def settle(task: asyncio.Task) -> None:
    """等一个可能被取消掉的流任务结束（被停止的轮次就是 cancel 掉的）。"""
    try:
        await asyncio.wait_for(task, timeout=5)
    except (asyncio.CancelledError, TimeoutError):
        pass


async def wait_for(predicate, timeout: float, what: str) -> None:  # noqa: ANN001
    """轮询等一个条件成立（测试用，不等就报错）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"等不到：{what}")


def service(agent: GeneralAgent) -> ChatService:
    """session 给 None 是安全的：workspace 用虚拟的 default，鉴权不查库。"""
    return ChatService(None, agent)  # type: ignore[arg-type]


async def part_a() -> None:
    print("== A 段：两个 agent 实例 = 两个 worker ==")

    print("-- A1 停止请求落到没有那一轮的进程 --")
    thread = f"t_cross_{SUFFIX}"
    worker_a, worker_b = GeneralAgent(model=SlowModel()), GeneralAgent(model=SlowModel())
    async with worker_a, worker_b:
        stream = await start_turn(worker_a, thread, "讲个长的")
        await asyncio.sleep(0.3)
        step(f"A 在跑（已流出 {worker_a.turns.get(thread).text!r}），B 上什么都没有")
        assert worker_b.turns.get(thread) is None, "B 不该有这一轮的本地登记"

        answer = await service(worker_b).stop(User(id=USER), thread_id=thread)
        step(f"B 调 /chat/stop 拿到答案 {answer!r}")
        assert answer == "（用户停止了本轮）" or answer.startswith("一"), (
            f"应该拿到 A 收尾的结果，实际 {answer!r}"
        )
        await settle(stream)
        step(f"A 的检查点已收尾：{await history(worker_a, thread)}")
        assert (await history(worker_a, thread)) == [("human", "讲个长的"), ("ai", answer)], (
            "A 那边应该已经被收尾，而且文本和 B 拿到的一致"
        )

        step("轮次登记已清掉")
        assert agent_db_execute(
            "select 1 from running_turns where thread_id = %s", (thread,)
        ) == []

        step("停止之后 B 能接着开新一轮（互斥已释放）")
        assert await worker_b.running_turns.open_turn(thread)  # type: ignore[union-attr]
        await worker_b.running_turns.close_turn(thread)  # type: ignore[union-attr]

    print("-- A2 别的 worker 在跑时，开轮次拿不到（跨进程 409）--")
    thread = f"t_busy_{SUFFIX}"
    worker_a, worker_b = GeneralAgent(model=SlowModel()), GeneralAgent(
        model=SlowModel()
    )
    async with worker_a, worker_b:
        stream = await start_turn(worker_a, thread, "占着")
        await asyncio.sleep(0.2)
        assert not await worker_b.running_turns.open_turn(thread), (  # type: ignore[union-attr]
            "A 开着这一轮，B 不该能开到"
        )
        step("B 开同一会话被表挡住（不是靠内存表）")
        await service(worker_a).stop(User(id=USER), thread_id=thread)
        await settle(stream)
        assert await worker_b.running_turns.open_turn(thread), "停掉之后应该能开"  # type: ignore[union-attr]
        await worker_b.running_turns.close_turn(thread)  # type: ignore[union-attr]

    print("-- A3 没人在跑时，request_stop 返回 None（不靠超时）--")
    worker = GeneralAgent(model=SlowModel())
    async with worker:
        rows = worker.running_turns
        assert rows is not None
        step(f"没登记过 -> {await rows.request_stop(f't_none_{SUFFIX}')}")
        assert await rows.request_stop(f"t_none_{SUFFIX}") is None
        assert await rows.open_turn(f"t_none_{SUFFIX}")
        step(f"登记了但没人认领 -> {await rows.request_stop(f't_none_{SUFFIX}')}")
        request = await rows.request_stop(f"t_none_{SUFFIX}")
        assert request is not None and request.answer is None, "有 owner，但还没收尾"
        await rows.drop_stop(f"t_none_{SUFFIX}")

    print("-- A4 心跳回收：假造一行心跳很旧的登记，sweep 之后能重新开 --")
    thread = f"t_stale_{SUFFIX}"
    worker = GeneralAgent(model=SlowModel())
    async with worker:
        rows = worker.running_turns
        assert rows is not None
        await rows.open_turn(thread)
        assert not await rows.open_turn(thread), "刚开的应该占着"
        agent_db_execute(
            "update running_turns set heartbeat_at = now() - %s where thread_id = %s",
            (TURN_HEARTBEAT_TTL * 2, thread),
        )
        await rows._sweep()  # 手动触发一次（正式跑是挂在 listener 空闲周期上）
        step(f"心跳停了 {TURN_HEARTBEAT_TTL} 的行被收掉")
        assert agent_db_execute(
            "select 1 from running_turns where thread_id = %s", (thread,)
        ) == []
        assert await rows.open_turn(thread), "回收之后应该能重新开"
        await rows.close_turn(thread)

    print("-- A5 孤儿检查点：跑到一半进程挂了，下一轮不会把两条 human 合并 --")
    thread = f"t_orphan_{SUFFIX}"
    worker_a, worker_b = GeneralAgent(model=SlowModel()), GeneralAgent(
        model=ToolModel(responses=[AIMessage("第二轮回答")])
    )
    async with worker_a, worker_b:
        stream = await start_turn(worker_a, thread, "跑到一半就没了")
        await asyncio.sleep(0.3)
        stream.cancel()  # 模拟进程被杀：任务没了，登记还在
        try:
            await stream
        except asyncio.CancelledError:
            pass
        step("模拟 worker 猝死：检查点停在半路，登记还占着")
        snapshot = await worker_a.memory._graph.aget_state(  # type: ignore[union-attr]
            {"configurable": {"thread_id": f"{USER}:{thread}"}}
        )
        assert snapshot.next, "应该停在半路"

        # 手动清掉心跳，等于等过了 TTL
        agent_db_execute(
            "update running_turns set heartbeat_at = now() - %s where thread_id = %s",
            (TURN_HEARTBEAT_TTL * 2, thread),
        )
        await worker_b.running_turns._sweep()  # type: ignore[union-attr]
        step("另一边的心跳回收把陈行清掉，会话可以继续")

        # 走 ChatService.open_turn（孤儿恢复就在那里），不是直接调 agent
        _, run = await service(worker_b).send(
            User(id=USER),
            workspace_id=WORKSPACE,
            message="接着聊",
            thread_id=thread,
            public_workspaces={},
        )
        messages = await history(worker_b, thread)
        step(f"续聊后 {messages}")
        assert messages[0][1] == "跑到一半就没了", "第一条 human 还在"
        assert messages[1][0] == "ai", (
            f"孤儿那一轮必须被收尾（否则两条 human 会合并成一次请求）：{messages}"
        )
        assert run.answer == "第二轮回答"

    print("-- A6 通知丢了：重连后靠补扫表兜底（_replay_pending）--")
    thread = f"t_lost_{SUFFIX}"
    worker_a = GeneralAgent(model=SlowModel())
    async with worker_a:
        rows_a = worker_a.running_turns
        assert rows_a is not None
        stream = await start_turn(worker_a, thread, "讲个长的")
        await asyncio.sleep(0.3)

        # 直接改表、**不发 NOTIFY** —— 这正是「通知丢了」留下的状态，
        # 只要另一条路（重连后补扫）还通，这一轮就还能被收尾。
        agent_db_execute(
            "update running_turns set stop_requested_at = now() where thread_id = %s",
            (thread,),
        )
        await asyncio.sleep(0.3)
        snapshot = await worker_a.memory._graph.aget_state(  # type: ignore[union-attr]
            {"configurable": {"thread_id": f"{USER}:{thread}"}}
        )
        step("没人收到通知 -> 这一轮还停在那里")
        assert snapshot.next, "没收到通知就不该有人收尾"

        # 从**服务器端**殺掉那条 LISTEN 连接（相当于 DB 重启/踢连接）：notifies() 会抛，
        # _listen_forever 接住重连。不用 conn.close() —— Windows 上关一个正被 notifies()
        # 使用的连接会报 WinError 10038。
        old = rows_a._listener_conn
        assert old is not None, "listener 应该已经订阅上了"
        killed = agent_db_execute(
            """
            select pg_terminate_backend(pid)
              from pg_stat_activity
             where pid <> pg_backend_pid()
               and query ilike %s
            """,
            (f"listen {CHANNEL}%",),
        )
        step(f"从服务器端踢掉 LISTEN 连接（{len(killed)} 条）")
        assert killed, "应该能找到那条 LISTEN 连接"
        await wait_for(
            lambda: rows_a._listener_conn is not None and rows_a._listener_conn is not old,
            10.0,
            "listener 重连",
        )
        step("LISTEN 连接断了又重连")

        answer = await rows_a.wait_stopped(thread, timeout=10.0)
        step(f"重连后补扫把这一轮收尾了，答案 {answer!r}")
        assert answer, "补扫应该让 owner 完成收尾并写回答案"
        await settle(stream)
        messages = await history(worker_a, thread)
        assert messages[-1][0] == "ai", f"收尾后应该有一条 AI 消息：{messages}"
        # 行要留着等请求方读走（这里就是请求方，所以 assert 完自己 drop）
        stored = agent_db_execute(
            "select answer, stopped_at is not null from running_turns where thread_id = %s",
            (thread,),
        )
        assert stored and stored[0][1], f"owner 应该把答案写回表：{stored}"
        assert await rows_a.wait_stopped(thread, timeout=1.0) == answer, "表里的答案要一致"
        await rows_a.drop_stop(thread)
        step("请求方读走结果后删行（下一轮开得起来）")


def main() -> None:
    cleanup()
    try:
        asyncio.run(part_a())
    finally:
        cleanup()
    print(f"\n全部通过：{checks} 项检查")


if __name__ == "__main__":
    main()