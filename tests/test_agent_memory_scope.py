"""验证「记忆按用户隔离 + /memories 只读」是否可行。

不需要数据库、不需要模型 API：用 InMemoryStore / InMemorySaver + 假模型。

1) 长期记忆隔离：``/memories/<user>/<ws>/x.md`` 经 CompositeBackend 剥掉路由前缀后，
   落在 store 命名空间 ``(<user>, "filesystem")``，换一个 user 查不到；
2) 只读：``permissions=[FilesystemPermission(operations=["write"], paths=["/memories/**"], mode="deny")]``
   下 write_file 被拒（read 放行）。

运行方式：``uv run python tests/test_agent_memory_scope.py``
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deepagents import FilesystemPermission, create_deep_agent  # noqa: E402
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend  # noqa: E402
from deepagents.middleware.filesystem import _check_fs_permission  # noqa: E402
from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.store.memory import InMemoryStore  # noqa: E402

MEMORY_ROUTE = "/memories/"
RULES = [FilesystemPermission(operations=["write"], paths=[MEMORY_ROUTE + "**"], mode="deny")]

store = InMemoryStore()


class FakeToolModel(FakeMessagesListChatModel):
    """假模型 + 空实现 bind_tools（deepagents 建图时会 bind，基类会抛 NotImplementedError）。"""

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return self


def backend_for(user_id: str) -> CompositeBackend:
    """与 agents/agent.py 同构：/memories/ 走 store，命名空间按用户切。"""
    return CompositeBackend(
        default=StateBackend(),
        routes={
            MEMORY_ROUTE: StoreBackend(
                namespace=lambda rt: (user_id, "filesystem"), store=store
            )
        },
    )


def main() -> None:
    print("== 1. 长期记忆：按用户命名空间隔离 ==")
    alice = backend_for("alice")
    res = alice.write("/memories/ws1/prefs.md", "喜欢简短回答")
    assert res.error is None, res
    keys = [item.key for item in store.search(("alice", "filesystem"))]
    print(f"  写入 {res.path} -> store[('alice','filesystem')] keys={keys}")
    assert keys == ["/ws1/prefs.md"], f"路由前缀应被剥掉，实际 {keys}"

    bob = backend_for("bob")
    got = bob.read("/memories/ws1/prefs.md")
    print(f"  bob 读同一路径 -> error={got.error!r}")
    assert got.error is not None, "不同用户命中了同一份文件，隔离失败"
    assert store.search(("bob", "filesystem")) == [], "bob 命名空间不该有数据"

    print("== 2. 只读规则：write deny / read allow ==")
    target = "/memories/alice/ws1/x.md"
    write_mode = _check_fs_permission(RULES, "write", target)
    read_mode = _check_fs_permission(RULES, "read", target)
    root_mode = _check_fs_permission(RULES, "write", "/memories")
    print(f"  write={write_mode} read={read_mode} 裸 '/memories' write={root_mode}")
    assert write_mode == "deny" and read_mode == "allow"

    print("== 3. 端到端：假模型真的调 write_file ==")
    model = FakeToolModel(
        responses=[
            AIMessage(
                "",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"file_path": target, "content": "hi"},
                        "id": "call_1",
                    }
                ],
            ),
            AIMessage("done"),
        ]
    )
    agent = create_deep_agent(
        model=model,
        backend=backend_for("alice"),
        permissions=RULES,
        checkpointer=InMemorySaver(),
    )
    out = agent.invoke(
        {"messages": [{"role": "user", "content": "记住 hi"}]},
        config={"configurable": {"thread_id": "alice:t1"}},
    )
    tools = [m for m in out["messages"] if m.type == "tool"]
    assert tools, "模型没触发工具调用，检查假模型"
    print(f"  write_file -> {tools[0].content}")
    assert "permission denied" in tools[0].content, tools[0].content
    # 第 1 步已经写过 ws1/prefs.md，这里只看被拒的那个 key 在不在
    assert not any("x.md" in item.key for item in store.search(("alice", "filesystem"))), \
        "被拒的写入不应落库"

    print("== 4. 忘记传 context 时的失败模式（命名空间工厂读 rt.context）==")
    agent2 = create_deep_agent(
        model=FakeToolModel(
            responses=[
                AIMessage(
                    "",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {"file_path": "/memories/ws1/prefs.md"},
                            "id": "call_2",
                        }
                    ],
                ),
                AIMessage("done"),
            ]
        ),
        backend=CompositeBackend(
            default=StateBackend(),
            routes={
                MEMORY_ROUTE: StoreBackend(
                    namespace=lambda rt: (rt.context.user_id, "filesystem"), store=store
                )
            },
        ),
        checkpointer=InMemorySaver(),
    )
    try:
        out2 = agent2.invoke(
            {"messages": [{"role": "user", "content": "读记忆"}]},
            config={"configurable": {"thread_id": "no-context"}},
        )
        msgs = [m.content for m in out2["messages"] if m.type == "tool"]
        print(f"  没报错，工具返回：{msgs}")
    except Exception as exc:  # noqa: BLE001
        print(f"  {type(exc).__name__}: {str(exc).splitlines()[0][:160]}")

    print("== 5. interrupt 模式：agent 写记忆需人工批准 ==")
    from langgraph.types import Command

    appr_store = InMemoryStore()
    appr_agent = create_deep_agent(
        model=FakeToolModel(
            responses=[
                AIMessage(
                    "",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"file_path": "/memories/prefs.md", "content": "喜欢简短回答"},
                            "id": "call_3",
                        }
                    ],
                ),
                AIMessage("已记住"),
            ]
        ),
        backend=CompositeBackend(
            default=StateBackend(),
            routes={
                MEMORY_ROUTE: StoreBackend(
                    namespace=lambda rt: ("alice", "ws1", "filesystem"), store=appr_store
                )
            },
        ),
        permissions=[
            FilesystemPermission(operations=["write"], paths=[MEMORY_ROUTE + "**"], mode="interrupt")
        ],
        checkpointer=InMemorySaver(),
    )
    cfg = {"configurable": {"thread_id": "alice:ws1:t2"}}
    out3 = appr_agent.invoke({"messages": [{"role": "user", "content": "记住偏好"}]}, config=cfg)
    req = (out3.get("__interrupt__") or [None])[0]
    assert req is not None, f"没有触发人工审批：{out3}"
    action = req.value["action_requests"][0]
    print(f"  待批准：{action['name']} {action['args']}")
    assert appr_store.search(("alice", "ws1", "filesystem")) == [], "批准前不应落库"

    out3 = appr_agent.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config=cfg)
    keys2 = [item.key for item in appr_store.search(("alice", "ws1", "filesystem"))]
    print(f"  批准后 keys={keys2}，最终回答={out3['messages'][-1].content!r}")
    assert keys2 == ["/prefs.md"], keys2

    print("\n全部断言通过。")


if __name__ == "__main__":
    main()
