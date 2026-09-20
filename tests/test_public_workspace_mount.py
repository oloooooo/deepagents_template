"""验证公共空间挂载 ``/public/`` 的隔离、共享与只读。

不需要数据库、不需要模型 API：``InMemoryStore`` + 假模型。

1) **隔离**：``ls /public/`` 只列得出当轮可见的公共空间；别人的空间连读都读不到；
2) **共享**：同一个公共空间的两个成员读到的是**同一份**内容（命名空间不含 user_id）；
3) **super**：可见范围是"全部"，不需要授权记录；
4) **只读**：``/public/**`` 的写操作被静态规则拒掉（super 也拒 —— 改内容走 REST）；
5) **端到端**：假模型真的调 ``ls`` / ``read_file`` / ``write_file``，走完整条链路。

运行方式：``uv run python tests/test_public_workspace_mount.py``
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deepagents import create_deep_agent  # noqa: E402
from deepagents.backends import CompositeBackend, StateBackend  # noqa: E402
from deepagents.middleware.filesystem import _check_fs_permission  # noqa: E402
from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.store.memory import InMemoryStore  # noqa: E402

from agents import public_workspace as pw  # noqa: E402
from agents.agent import AgentContext  # noqa: E402

REAL_GET_RUNTIME = pw.get_runtime

A_ID, B_ID = "pa", "pb"
ALICE = {"proj-a": A_ID}  # 只被授权 proj-a
SUPER = {"proj-a": A_ID, "proj-b": B_ID}  # super 全部可见

store = InMemoryStore()


class FakeToolModel(FakeMessagesListChatModel):
    """假模型 + 空实现 bind_tools（deepagents 建图时会 bind，基类会抛 NotImplementedError）。"""

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return self


def use(**context_kwargs) -> pw.PublicMountBackend:
    """切到"当轮上下文"并返回挂载 backend。

    backend 在图里是**单例**（启动时建一次），变的是每轮的 ``AgentContext``，所以这里
    每次操作前都要重设 —— 一个 backend 实例 + 不断切换的上下文，才是真实形态。
    ``_allowed()`` 走 ``get_runtime().context``（图执行期才有），直接调时用假 runtime 顶替。
    """
    pw.get_runtime = lambda: SimpleNamespace(context=AgentContext(**context_kwargs))
    return backend


backend = pw.PublicMountBackend(store)


def paths(result) -> list[str]:
    return [entry["path"] for entry in (result.entries or [])]


def main() -> None:
    print("== 0. 准备：两个公共空间各写一份内容（走 REST 侧的 store 接口）==")
    import asyncio

    public = pw.PublicWorkspaceStore(store)
    asyncio.run(public.awrite(A_ID, "notes.md", "proj-a 的内容"))
    asyncio.run(public.awrite(B_ID, "secret.md", "proj-b 的内容"))
    print(f"  proj-a={asyncio.run(public.alist(A_ID))} proj-b={asyncio.run(public.alist(B_ID))}")
    # 命名空间不含 user_id：谁写进去都是同一份
    assert asyncio.run(public.aread(A_ID, "notes.md")) == "proj-a 的内容"

    print("== 1. 隔离：成员只看得见自己被授权的公共空间 ==")
    alice = use(user_id="alice", public_workspaces=ALICE)
    assert paths(alice.ls("/")) == ["/proj-a/"], paths(alice.ls("/"))
    print(f"  alice ls /public/ -> {paths(alice.ls('/'))}")

    got = alice.read("/proj-a/notes.md")
    print(f"  alice 读 /proj-a/notes.md -> {got.file_data['content']!r}")
    assert got.error is None and got.file_data["content"] == "proj-a 的内容"

    denied = alice.read("/proj-b/secret.md")
    print(f"  alice 读 /proj-b/secret.md -> error={denied.error!r}")
    assert denied.error is not None, "没被授权的公共空间竟然读到了"
    assert alice.ls("/proj-b").error is not None, "没被授权的目录不该列得出来"

    print("== 2. 共享：同一公共空间的另一个成员读到同一份 ==")
    bob = use(user_id="bob", public_workspaces=ALICE)
    shared = bob.read("/proj-a/notes.md")
    print(f"  bob 读 /proj-a/notes.md -> {shared.file_data['content']!r}")
    assert shared.file_data["content"] == "proj-a 的内容"

    print("== 3. super：可见范围是全部，没有授权记录也看得见 ==")
    root = use(user_id="root", public_workspaces=SUPER)
    assert paths(root.ls("/")) == ["/proj-a/", "/proj-b/"], paths(root.ls("/"))
    print(f"  super ls /public/ -> {paths(root.ls('/'))}")
    assert root.read("/proj-b/secret.md").error is None

    print("== 4. glob / grep 跨空间合并，且不越权 ==")
    found = use(user_id="alice", public_workspaces=ALICE).glob("*.md")
    print(f"  alice glob '*.md' -> {[m['path'] for m in (found.matches or [])]}")
    assert [m["path"] for m in (found.matches or [])] == ["/proj-a/notes.md"]
    hits = use(user_id="root", public_workspaces=SUPER).grep("内容")
    print(f"  super grep '内容' -> {sorted(m['path'] for m in (hits.matches or []))}")
    assert sorted(m["path"] for m in (hits.matches or [])) == [
        "/proj-a/notes.md",
        "/proj-b/secret.md",
    ]
    assert use(user_id="alice", public_workspaces=ALICE).grep("内容").matches == [
        {"path": "/proj-a/notes.md", "line": 1, "text": "proj-a 的内容"}
    ]

    print("== 5. 只读规则：/public/** 的 write 一律 deny（super 也 deny）==")
    target = "/public/proj-a/notes.md"
    for who in ("alice", "root"):
        assert _check_fs_permission(pw.PUBLIC_PERMISSIONS, "write", target) == "deny"
    assert _check_fs_permission(pw.PUBLIC_PERMISSIONS, "read", target) == "allow"
    assert _check_fs_permission(pw.PUBLIC_PERMISSIONS, "write", "/public") == "deny"
    print("  write=deny read=allow 裸 '/public' write=deny")

    print("== 6. 端到端：假模型真的调 ls / read_file / write_file ==")
    pw.get_runtime = REAL_GET_RUNTIME  # 图执行期用真的 runtime
    agent = create_deep_agent(
        model=FakeToolModel(
            responses=[
                AIMessage("", tool_calls=[{"name": "ls", "args": {"path": "/public/"}, "id": "c1"}]),
                AIMessage("", tool_calls=[{"name": "read_file", "args": {"file_path": "/public/proj-a/notes.md"}, "id": "c2"}]),
                AIMessage("", tool_calls=[{"name": "read_file", "args": {"file_path": "/public/proj-b/secret.md"}, "id": "c3"}]),
                AIMessage("", tool_calls=[{"name": "write_file", "args": {"file_path": "/public/proj-a/hack.md", "content": "x"}, "id": "c4"}]),
                AIMessage("done"),
            ]
        ),
        backend=CompositeBackend(
            default=StateBackend(), routes={pw.PUBLIC_ROUTE: pw.PublicMountBackend(store)}
        ),
        permissions=pw.PUBLIC_PERMISSIONS,
        checkpointer=InMemorySaver(),
        context_schema=AgentContext,
    )
    out = agent.invoke(
        {"messages": [{"role": "user", "content": "看看公共空间"}]},
        config={"configurable": {"thread_id": "alice:t1"}},
        context=AgentContext(user_id="alice", public_workspaces=ALICE),
    )
    tools = [m.content for m in out["messages"] if m.type == "tool"]
    for content in tools:
        print(f"  -> {content.splitlines()[0][:100]}")
    assert len(tools) == 4, tools
    assert "proj-a" in tools[0] and "proj-b" not in tools[0], tools[0]
    assert "proj-a 的内容" in tools[1], tools[1]
    assert "proj-a 的内容" not in tools[2] and "not found" in tools[2].lower() or "不存在" in tools[2], tools[2]
    assert "permission denied" in tools[3], tools[3]
    assert not any(
        "hack.md" in item.key for item in store.search(pw.public_namespace(A_ID))
    ), "被拒的写入不该落库"

    print("\n全部断言通过。")


if __name__ == "__main__":
    main()
