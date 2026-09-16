"""验证 GeneralAgent 的记忆行为（需要本机 PostgreSQL 的 agents 库，不需要模型 API key）。

用假模型（顺序返回预设消息）跑真图、真检查点、真 store，验证：

1. agent 写 /memories/ 被 interrupt 拦下，批准后落库；
2. 落库位置按 (user_id, workspace_id) 隔离，换个用户/空间就看不见；
3. AgentMemory 的写入四件套是用户侧直写，不经模型；
4. 短期记忆（检查点）按 ``user_id:thread_id`` 隔离。

运行方式：``uv run python tests/test_agent_chat_memory.py``
"""

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage  # noqa: E402

from agents.agent import GeneralAgent  # noqa: E402

# 每次跑用新命名空间，不干扰上一次残留的检查点/记忆
SUFFIX = uuid4().hex[:8]
USER, WORKSPACE, THREAD = f"u_scope_{SUFFIX}", f"ws_scope_{SUFFIX}", f"t_scope_{SUFFIX}"


class FakeToolModel(FakeMessagesListChatModel):
    """假模型 + 空实现 bind_tools（deepagents 建图时会 bind）。"""

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return self


def model_for(path: str) -> FakeToolModel:
    return FakeToolModel(
        responses=[
            AIMessage(
                "",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"file_path": path, "content": "喜欢简短回答"},
                        "id": "call_1",
                    }
                ],
            ),
            AIMessage("已记住"),
        ]
    )


async def main() -> None:
    async with GeneralAgent(model=model_for("/memories/prefs.md")) as agent:
        assert agent.memory is not None

        print("== 1. 首轮：agent 写记忆被拦下 ==")
        run = await agent.ainvoke("记住我的偏好", thread_id=THREAD, user_id=USER, workspace_id=WORKSPACE)
        assert run.interrupt is not None, f"没触发人工批准：{run}"
        action = run.interrupt["action_requests"][0]
        print(f"  待批准 {action['name']} {action['args']}")
        assert await agent.memory.alist_memories(USER, WORKSPACE) == [], "批准前不该落库"

        print("== 2. 批准后落库 ==")
        run = await agent.ainvoke(
            thread_id=THREAD,
            user_id=USER,
            workspace_id=WORKSPACE,
            resume={"decisions": [{"type": "approve"}]},
        )
        print(f"  answer={run.answer!r} interrupt={run.interrupt}")
        assert run.answer == "已记住" and run.interrupt is None
        assert await agent.memory.alist_memories(USER, WORKSPACE) == ["/memories/prefs.md"]

        print("== 3. 换用户 / 换空间看不见 ==")
        assert await agent.memory.alist_memories(USER, "ws_other") == []
        assert await agent.memory.alist_memories("u_other", WORKSPACE) == []
        assert await agent.memory.aread_memory("u_other", WORKSPACE, "prefs.md") is None

        print("== 4. 用户侧直写：不经模型、不需批准 ==")
        path = await agent.memory.awrite_memory(USER, WORKSPACE, "note.md", "用户自己写的")
        assert path == "/memories/note.md", path
        assert await agent.memory.aread_memory(USER, WORKSPACE, "/memories/note.md") == "用户自己写的"
        assert await agent.memory.adelete_memory(USER, WORKSPACE, "note.md") is True
        assert await agent.memory.adelete_memory(USER, WORKSPACE, "note.md") is False
        await agent.memory.adelete_memory(USER, WORKSPACE, "prefs.md")

        print("== 5. 短期记忆按用户隔离 ==")
        state = await agent.memory.aget_state(THREAD, USER)
        print(f"  {state['conversation_id']} messages={state['messages']} answer={state['answer']!r}")
        assert state["conversation_id"] == f"{USER}:{THREAD}"
        # 别人的 thread_id 即使猜到了也拼不出同一个会话键，检查点里查不到消息
        other = await agent.memory.aget_state(THREAD, "u_other")
        assert other["messages"] == 0 and other["answer"] == "", other

        print("== 6. 会话元数据（归属 + 空间）已在检查点里 ==")
        meta = await agent.memory.aget_meta(THREAD, USER)
        print(f"  {meta}")
        assert meta is not None and meta["user_id"] == USER, meta
        assert meta["workspace_id"] == WORKSPACE, meta
        assert await agent.memory.aget_meta("t_missing", USER) is None

        print("== 7. 流式：写记忆时收到 interrupt 事件 ==")
        async with GeneralAgent(model=model_for("/memories/stream.md")) as streamer:
            kinds = [
                event.kind
                async for event in streamer.astream(
                    "记住", thread_id="t_stream", user_id=USER, workspace_id=WORKSPACE
                )
            ]
            print(f"  事件序列 {kinds}")
            assert kinds[-2:] == ["interrupt", "done"], kinds
            await streamer.memory.adelete_memory(USER, WORKSPACE, "stream.md")

    print("\n全部断言通过。")


if __name__ == "__main__":
    asyncio.run(main())
