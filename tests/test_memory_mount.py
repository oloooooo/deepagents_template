"""验证记忆挂载 ``/memories/``：一个人一个命名空间 ``(user_id, "filesystem")``。

不需要数据库、不需要模型 API：``InMemoryStore`` + 假模型。

1) 清单：``ls /memories/`` 直接列出文件，没有格子层；
2) 隔离：命名空间是 ``(user_id, "filesystem")``，换个人读同一路径读不到；
3) 静态规则：写 ``/memories/**`` 一律 interrupt（读放行），裸 ``/memories`` 也拦；
4) 端到端：假模型真的调 ls / read_file / write_file，走完整条链路（含 interrupt → 批准 → 落库）；
5) 批量上传一律拒（它是写，却绕开人工批准）。

运行方式：``uv run python tests/test_memory_mount.py``
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deepagents import create_deep_agent  # noqa: E402
from deepagents.backends import CompositeBackend, StateBackend  # noqa: E402
from deepagents.backends.utils import create_file_data  # noqa: E402
from deepagents.middleware.filesystem import _check_fs_permission  # noqa: E402
from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.store.memory import InMemoryStore  # noqa: E402
from langgraph.types import Command  # noqa: E402

import deepagents.backends.store as store_mod  # noqa: E402
from agents.agent import (  # noqa: E402
    MEMORY_PERMISSIONS,
    MEMORY_ROUTE,
    AgentContext,
    memory_mount,
)

REAL_GET_RUNTIME = store_mod.get_runtime

store = InMemoryStore()
backend = memory_mount(store)


class FakeToolModel(FakeMessagesListChatModel):
    """假模型 + 空实现 bind_tools（deepagents 建图时会 bind，基类会抛 NotImplementedError）。"""

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return self


def use(user_id: str = "alice"):
    """切到"当轮上下文"并返回挂载 backend。

    backend 在图里是**单例**（启动时建一次），变的是每轮的 ``AgentContext``，所以这里每次
    操作前都要重设 —— 一个 backend 实例 + 不断切换的上下文，才是真实形态。
    ``_get_namespace()`` 走 ``store.get_runtime()``（图执行期才有），直接调时用假 runtime 顶替。
    """
    store_mod.get_runtime = lambda: SimpleNamespace(
        context=AgentContext(user_id=user_id)
    )
    return backend


def paths(result) -> list[str]:  # noqa: ANN001
    return [entry["path"] for entry in (result.entries or [])]


def keys(user_id: str) -> list[str]:
    return [item.key for item in store.search((user_id, "filesystem"))]


def main() -> None:
    print("== 0. 准备：放一份资料（相当于用户走 /memories/write 投喂）==")
    store.put(("alice", "filesystem"), "/notes.md", create_file_data("alice 的资料"))
    print(f"  alice -> {keys('alice')}")

    print("== 1. 清单：直接列文件，没有格子层 ==")
    alice = use()
    assert paths(alice.ls("/")) == ["/notes.md"], paths(alice.ls("/"))
    print(f"  ls /memories/ -> {paths(alice.ls('/'))}")

    print("== 2. 隔离：命名空间是 (user_id, 'filesystem')，换个人读同一路径读不到 ==")
    got = use().read("/notes.md")
    print(f"  alice 读 /notes.md -> {got.file_data['content']!r}")
    assert got.error is None and got.file_data["content"] == "alice 的资料"
    denied = use(user_id="bob").read("/notes.md")
    print(f"  bob 读同一路径 -> error={denied.error!r}")
    assert denied.error is not None, "不同用户命中了同一份文件"
    assert keys("bob") == [], "bob 的命名空间不该有数据"

    print("== 3. 静态规则：写 /memories/** 一律 interrupt，读放行 ==")
    for target in (
        f"{MEMORY_ROUTE}prefs.md",
        f"{MEMORY_ROUTE}notes/a.md",
        MEMORY_ROUTE.rstrip("/"),
    ):
        actual = _check_fs_permission(MEMORY_PERMISSIONS, "write", target)
        assert actual == "interrupt", (target, actual)
    print("  裸 '/memories' 与任意子路径 write=interrupt")
    assert (
        _check_fs_permission(MEMORY_PERMISSIONS, "read", f"{MEMORY_ROUTE}notes.md")
        == "allow"
    )

    print("== 4. 端到端：假模型真的调 ls / read_file / write_file ==")
    store_mod.get_runtime = REAL_GET_RUNTIME  # 图执行期用真的 runtime
    agent = create_deep_agent(
        model=FakeToolModel(
            responses=[
                AIMessage("", tool_calls=[{"name": "ls", "args": {"path": "/memories/"}, "id": "c1"}]),
                AIMessage("", tool_calls=[{"name": "read_file", "args": {"file_path": "/memories/notes.md"}, "id": "c2"}]),
                AIMessage("", tool_calls=[{"name": "write_file", "args": {"file_path": "/memories/prefs.md", "content": "喜欢简短回答"}, "id": "c3"}]),
                AIMessage("done"),
            ]
        ),
        backend=CompositeBackend(
            default=StateBackend(), routes={MEMORY_ROUTE: memory_mount(store)}
        ),
        permissions=MEMORY_PERMISSIONS,
        checkpointer=InMemorySaver(),
        context_schema=AgentContext,
    )
    context = AgentContext(user_id="alice")
    cfg = {"configurable": {"thread_id": "alice:mine:t1"}}
    out = agent.invoke(
        {"messages": [{"role": "user", "content": "看看我的记忆"}]}, config=cfg, context=context
    )
    tools = [m.content for m in out["messages"] if m.type == "tool"]
    for content in tools:
        print(f"  -> {content.splitlines()[0][:90]}")
    assert len(tools) == 2, tools  # 第 3 个（写记忆）被拦下等人批准，还没有工具结果
    assert "notes.md" in tools[0], tools[0]
    assert "alice 的资料" in tools[1], tools[1]
    assert keys("alice") == ["/notes.md"], "批准前不该落库"

    step_3 = (out.get("__interrupt__") or [None])[0]
    assert step_3 is not None, f"没触发人工审批：{out.keys()}"
    action = step_3.value["action_requests"][0]
    print(f"  待批准：{action['name']} {action['args']}")
    assert action["args"]["file_path"] == "/memories/prefs.md", action

    out2 = agent.invoke(
        Command(resume={"decisions": [{"type": "approve"}]}), config=cfg, context=context
    )
    print(f"  批准后 alice -> {keys('alice')}，回答={out2['messages'][-1].content!r}")
    assert sorted(keys("alice")) == ["/notes.md", "/prefs.md"]

    print("== 5. 批量上传一律拒（它是写，却绕开人工批准）==")
    downloader = use()
    uploaded = downloader.upload_files([("/sneak.md", b"x")])
    print(f"  upload -> {uploaded}")
    assert [(r.path, r.error) for r in uploaded] == [
        ("/sneak.md", "permission_denied")
    ], uploaded
    assert "/sneak.md" not in keys("alice"), "批量上传不该落库"

    print("\n全部断言通过。")


if __name__ == "__main__":
    main()
