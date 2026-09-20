"""验证聊天停止（``/chat/stop``）：短期记忆就地收尾、可续聊、幂等、互斥、鉴权。

前置条件：本机 PostgreSQL 可连（``agents`` 库存在）、``user_related`` 已 ``alembic upgrade head``。
运行方式：``uv run python tests/test_chat_stop.py``（自建自清）。

分两层验：

- **A 段（agent 层）**：``AgentMemory.astop`` 的四种入口 —— 工具执行中途中止、模型生成
  中途中止、正等人批准、已经跑完。重点锁住「下一轮不会把停掉的那一轮和新消息合并成一次请求」
  这个原本存在的 bug。
- **B 段（HTTP 层）**：``/chat/stop`` 的鉴权、幂等、409 互斥，以及流式跑到一半按停止之后
  历史是不是收尾正确、能不能接着聊。
"""

import asyncio
import sys
import threading
import time
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from langchain_core.language_models import BaseChatModel  # noqa: E402
from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage  # noqa: E402
from langchain_core.outputs import (  # noqa: E402
    ChatGeneration,
    ChatGenerationChunk,
    ChatResult,
)
from langchain_core.tools import tool  # noqa: E402

from agents.agent import STOP_PLACEHOLDER, GeneralAgent, TurnRegistry  # noqa: E402
from agents.config import AgentPostgreConfig  # noqa: E402
from config import app_config  # noqa: E402
from main import app  # noqa: E402

SUFFIX = uuid4().hex[:8]
ACCOUNT_FAMILY = "chat_stop_"
ACCOUNT = f"{ACCOUNT_FAMILY}{SUFFIX}"
PASSWORD = "Passw0rd!123"

# agent 层的固定标识（不建用户，直接借 (user_id, workspace_id) 命名空间）
USER, WORKSPACE = f"u_stop_{SUFFIX}", f"ws_stop_{SUFFIX}"

checks = 0


def step(name: str) -> None:
    global checks
    checks += 1
    print(f"  [{checks:02d}] {name}")


class ToolModel(FakeMessagesListChatModel):
    """假模型：第一轮调 ``slow_tool``（好在中途掐掉），之后回一句话。"""

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return self


class SlowStreamModel(BaseChatModel):
    """假模型：一次吐 ``CHUNKS`` 个 chunk，每个隔 ``DELAY`` 秒，好在中途按停止。"""

    CHUNKS: ClassVar[str] = "一二三四五六七八九十"
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


class EchoModel(SlowStreamModel):
    """HTTP 段用：把用户的话回声出去，便于断言「续聊不合并」。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(f"收到：{messages[-1].content}"))]
        )


@tool
async def slow_tool(x: str) -> str:
    """很慢的工具（测试用）。"""
    await asyncio.sleep(30)
    return f"done {x}"


SCRIPTED_MODEL = EchoModel()


def agent_db_execute(sql: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(AgentPostgreConfig().uri, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def db_execute(sql: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(app_config.postgresql.user.uri, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def cleanup() -> None:
    """先按账号族拿 user_id，再清 agent 库（thread_id 前缀是 user_id，不是账号名）。"""
    like = f"{ACCOUNT_FAMILY}%"
    user_ids = [row[0] for row in db_execute("select id from users where account like %s", (like,))]
    prefixes = [f"{USER}:%", *[f"{uid}:%" for uid in user_ids]]
    for prefix in prefixes:
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            agent_db_execute(f"delete from {table} where thread_id like %s", (prefix,))
    for prefix in [f"{USER}.%", *[f"{uid}.%" for uid in user_ids]]:
        agent_db_execute("delete from store where prefix like %s", (prefix,))
    db_execute("delete from users where account like %s", (like,))


async def abort_midway(agent: GeneralAgent, *, thread_id: str, message: str, after: float) -> None:
    """起一轮流式对话，``after`` 秒后掐掉消费方（模拟用户按停止）。"""

    async def consume() -> None:
        async for _ in agent.astream(
            message, thread_id=thread_id, user_id=USER, workspace_id=WORKSPACE
        ):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(after)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def history(agent: GeneralAgent, thread_id: str) -> list[tuple[str, str]]:
    assert agent.memory is not None
    messages = await agent.memory.alist_messages(thread_id, USER)
    return [(message["role"], message["content"]) for message in messages]


async def raw_state(agent: GeneralAgent, thread_id: str) -> tuple[tuple[str, ...], bool]:
    """(next, 有没有待批准的请求) —— 收尾是否彻底看这两个。"""
    assert agent.memory is not None
    snapshot = await agent.memory._graph.aget_state(
        {"configurable": {"thread_id": f"{USER}:{thread_id}"}}
    )
    return tuple(snapshot.next), bool(snapshot.interrupts)


async def part_a() -> None:
    print("== A 段：AgentMemory.astop 的四种入口 ==")

    print("-- A1 工具执行中途中止（悬挂一条只有 tool_calls 的空 AI 消息）--")
    thread = f"t_tool_{SUFFIX}"
    agent = GeneralAgent(
        model=ToolModel(
            responses=[
                AIMessage(
                    "",
                    tool_calls=[{"name": "slow_tool", "args": {"x": "a"}, "id": "c1"}],
                ),
                AIMessage("下一轮回答"),
            ]
        ),
        tools=[slow_tool],
    )
    async with agent:
        assert agent.memory is not None
        await abort_midway(agent, thread_id=thread, message="跑", after=0.8)
        pending, _ = await raw_state(agent, thread)
        step(f"中止后 next={pending}（这一轮没跑完）")
        assert pending, "中止后应该停在半路"

        answer = await agent.memory.astop(thread, USER, WORKSPACE)
        step(f"astop 返回 {answer!r}")
        assert answer == STOP_PLACEHOLDER, "一个字都没流出来，应该用占位文案"
        assert await history(agent, thread) == [("human", "跑"), ("ai", STOP_PLACEHOLDER)], (
            "空的 AI 消息应该被删掉，只留一条收尾消息"
        )
        pending, interrupted = await raw_state(agent, thread)
        assert not pending and not interrupted, f"收尾后应该是终点：next={pending}"

        await agent.ainvoke(
            "接着聊", thread_id=thread, user_id=USER, workspace_id=WORKSPACE
        )
        messages = await history(agent, thread)
        step(f"续聊后 {messages}")
        assert messages == [
            ("human", "跑"),
            ("ai", STOP_PLACEHOLDER),
            ("human", "接着聊"),
            ("ai", "下一轮回答"),
        ], "续聊应该接在收尾之后，不能把两条 human 合并成一次请求"

    print("-- A2 模型生成中途中止（已流出的文本必须写回历史）--")
    thread = f"t_stream_{SUFFIX}"
    agent = GeneralAgent(model=SlowStreamModel())
    async with agent:
        assert agent.memory is not None
        await abort_midway(agent, thread_id=thread, message="讲个长的", after=0.3)
        pending, _ = await raw_state(agent, thread)
        step(f"中止后 next={pending}，历史={await history(agent, thread)}")
        assert pending, "中止后应该停在半路"
        assert await history(agent, thread) == [("human", "讲个长的")], (
            "生成中途的增量不落检查点，历史里只剩 human 消息"
        )

        answer = await agent.memory.astop(thread, USER, WORKSPACE, text="一二三")
        step(f"astop(text='一二三') 返回 {answer!r}")
        assert answer == "一二三"
        assert await history(agent, thread) == [("human", "讲个长的"), ("ai", "一二三")]

        await agent.ainvoke("在吗", thread_id=thread, user_id=USER, workspace_id=WORKSPACE)
        messages = await history(agent, thread)
        step(f"续聊后 {messages}")
        assert messages[-2:] == [("human", "在吗"), ("ai", "完整回答")], (
            "「讲个长的」已经有回答了，不该再和新消息合并"
        )
        assert messages[1] == ("ai", "一二三"), "已流出的部分文本要留在历史里"

    print("-- A3 正等人批准时停止（丢弃待批准的请求）--")
    thread = f"t_hitl_{SUFFIX}"
    agent = GeneralAgent(
        model=ToolModel(
            responses=[
                AIMessage(
                    "",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"file_path": "/memories/x.md", "content": "hi"},
                            "id": "w1",
                        }
                    ],
                ),
                AIMessage("下一轮回答"),
            ]
        )
    )
    async with agent:
        assert agent.memory is not None
        run = await agent.ainvoke(
            "记一下", thread_id=thread, user_id=USER, workspace_id=WORKSPACE
        )
        step(f"第一轮被拦下等人批准：{run.interrupt is not None}")
        assert run.interrupt is not None, "写 /memories/ 应该触发人工批准"
        _, interrupted = await raw_state(agent, thread)
        assert interrupted, "应该有待批准的请求"

        answer = await agent.memory.astop(thread, USER, WORKSPACE)
        step(f"astop 返回 {answer!r}")
        assert answer == STOP_PLACEHOLDER
        pending, interrupted = await raw_state(agent, thread)
        assert not pending and not interrupted, f"待批准请求应该被丢弃：next={pending}"
        assert await history(agent, thread) == [("human", "记一下"), ("ai", STOP_PLACEHOLDER)]

    print("-- A4 已经跑完的轮次：幂等，不污染历史 --")
    thread = f"t_done_{SUFFIX}"
    agent = GeneralAgent(model=EchoModel())
    async with agent:
        assert agent.memory is not None
        await agent.ainvoke("你好", thread_id=thread, user_id=USER, workspace_id=WORKSPACE)
        before = await history(agent, thread)
        answer = await agent.memory.astop(thread, USER, WORKSPACE, text="不该出现")
        step(f"astop 返回 {answer!r}，历史 {await history(agent, thread)}")
        assert answer == "收到：你好", "跑完的轮次应该返回原来的回答"
        assert await history(agent, thread) == before, "跑完的轮次不该被塞占位文案"

    print("-- A5 TurnRegistry：互斥 + 自愈 --")
    registry = TurnRegistry()
    first = registry.reserve("t")
    step("第一次 reserve 成功")
    assert first is not None
    assert registry.reserve("t") is None, "同一 thread 第二次 reserve 应该失败（409）"
    assert registry.get("t") is first
    registry.release(first)
    assert registry.reserve("t") is not None, "释放后应该能再占"


def part_b() -> None:
    print("== B 段：/chat/stop 的 HTTP 契约 ==")
    GeneralAgent._build_model = lambda self: SCRIPTED_MODEL  # type: ignore[method-assign]
    with TestClient(app) as client:
        assert (
            client.post(
                "/auth/register",
                json={"account": ACCOUNT, "email": f"{ACCOUNT}@example.com", "password": PASSWORD},
            ).status_code
            == 201
        )
        headers = {
            "Authorization": "Bearer "
            + client.post(
                "/auth/login", json={"account": ACCOUNT, "password": PASSWORD}
            ).json()["access_token"]
        }

        step("未登录 POST /chat/stop -> 401")
        assert client.post("/chat/stop", json={"thread_id": "t"}).status_code == 401

        step("请求体塞 user_id -> 422（extra=forbid）")
        assert (
            client.post(
                "/chat/stop", json={"thread_id": "t", "user_id": "u"}, headers=headers
            ).status_code
            == 422
        )

        step("不存在的会话 -> 404（不泄露存在性）")
        assert (
            client.post(
                "/chat/stop", json={"thread_id": f"nope_{SUFFIX}"}, headers=headers
            ).status_code
            == 404
        )

        thread = f"t_http_done_{SUFFIX}"
        step("跑完的会话再按停止 -> 200 幂等，历史不变")
        sent = client.post(
            "/chat/send", json={"message": "你好", "thread_id": thread}, headers=headers
        )
        assert sent.status_code == 200, sent.text
        before = client.get(f"/chat/history/{thread}", headers=headers).json()["messages"]
        stopped = client.post("/chat/stop", json={"thread_id": thread}, headers=headers)
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["answer"] == sent.json()["answer"]
        after = client.get(f"/chat/history/{thread}", headers=headers).json()["messages"]
        assert after == before, "跑完的会话不该被停止污染"

        print("-- B1 流式跑到一半按停止 --")
        thread = f"t_http_stream_{SUFFIX}"
        box: dict = {}

        def stream() -> None:
            # 被 /chat/stop 停掉时这一轮会被取消，客户端拿到的是一条断掉的流 —— 预期之内
            try:
                box["resp"] = client.post(
                    "/chat/stream",
                    json={"message": "讲个长的", "thread_id": thread},
                    headers=headers,
                )
            except Exception as exc:  # noqa: BLE001
                box["error"] = exc

        worker = threading.Thread(target=stream)
        worker.start()
        time.sleep(0.4)  # 让流先吐几个 chunk
        step("流式进行中 POST /chat/stop -> 200")
        stopped = client.post("/chat/stop", json={"thread_id": thread}, headers=headers)
        assert stopped.status_code == 200, stopped.text
        partial = stopped.json()["answer"]
        step(f"stop 返回 {partial!r}")
        assert partial and partial != STOP_PLACEHOLDER, "应该拿到已经流出去的部分文本"
        worker.join(timeout=10)
        assert not worker.is_alive(), "被停止后流应该结束"

        messages = client.get(f"/chat/history/{thread}", headers=headers).json()["messages"]
        step(f"收尾后的历史 {[(m['role'], m['content']) for m in messages]}")
        assert [m["role"] for m in messages] == ["human", "ai"], "这一轮应该被就地收尾"
        assert messages[1]["content"] == partial, "历史里的文本要和 stop 返回的一致"
        assert partial.startswith("一"), "部分文本应该是从开头攒起来的"

        step("停止之后接着聊 -> 两轮分开，不合并")
        again = client.post(
            "/chat/send", json={"message": "在吗", "thread_id": thread}, headers=headers
        )
        assert again.status_code == 200, again.text
        messages = client.get(f"/chat/history/{thread}", headers=headers).json()["messages"]
        assert [m["role"] for m in messages] == ["human", "ai", "human", "ai"], (
            f"应该是两轮：{[(m['role'], m['content']) for m in messages]}"
        )
        assert messages[2]["content"] == "在吗" and "收到：在吗" in messages[3]["content"]

        print("-- B2 同一会话并发第二轮 -> 409 --")
        thread = f"t_http_busy_{SUFFIX}"
        worker = threading.Thread(target=stream)
        worker.start()
        time.sleep(0.4)
        busy = client.post(
            "/chat/send", json={"message": "插队", "thread_id": thread}, headers=headers
        )
        step(f"在跑的会话上再发一条 -> {busy.status_code}")
        assert busy.status_code == 409, busy.text
        assert client.post("/chat/stop", json={"thread_id": thread}, headers=headers).status_code == 200
        worker.join(timeout=10)
        assert not worker.is_alive()

        step("停止之后再发 -> 200（互斥已释放）")
        assert (
            client.post(
                "/chat/send", json={"message": "现在可以了", "thread_id": thread}, headers=headers
            ).status_code
            == 200
        )


def main() -> None:
    cleanup()
    try:
        asyncio.run(part_a())
        part_b()
    finally:
        cleanup()
    print(f"\n全部通过：{checks} 项检查")


if __name__ == "__main__":
    main()
