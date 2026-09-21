"""验证记忆挂载 ``/memories/``：按业务空间分格的目录树，且只有 default 那一格能写。

不需要数据库、不需要模型 API：``InMemoryStore`` + 假模型。

1) 清单：``ls /memories/`` 只列得出当轮可见的格子（成员关系 + 虚拟 default），空空间也列；
2) 隔离：命名空间是 ``(user_id, 空间)``，换个人读同一个格子读不到；
3) 只写 default：其它格子的 write / edit / delete 一律被拒（静态 deny + 挂载层兜底）；
4) 扇出：挂载根上的 glob / grep 一次搜多个格子，命中路径带格子名前缀；
5) 端到端：假模型真的调 ls / read_file / write_file，走完整条链路（含 interrupt → 批准 → 落库）；
6) 批量下载按路径选格子（中间件用得到），批量上传一律拒（写不能绕开人工批准）。

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

import agents.fanout as fanout  # noqa: E402
from agents.agent import (  # noqa: E402
    MEMORY_DENIED,
    MEMORY_PERMISSIONS,
    MEMORY_ROUTE,
    AgentContext,
    memory_mount,
)

REAL_GET_RUNTIME = fanout.get_runtime

ALICE_SPACES = {"default": "default", "proj-a": "wsa", "proj-b": "wsb"}
"""alice 参与的三个格子：虚拟 default + 两个业务空间（名字 -> id）。"""

store = InMemoryStore()
backend = memory_mount(store)


class FakeToolModel(FakeMessagesListChatModel):
    """假模型 + 空实现 bind_tools（deepagents 建图时会 bind，基类会抛 NotImplementedError）。"""

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return self


def use(user_id: str = "alice", spaces: dict[str, str] | None = None):
    """切到"当轮上下文"并返回挂载 backend。

    backend 在图里是**单例**（启动时建一次），变的是每轮的 ``AgentContext``，所以这里每次
    操作前都要重设 —— 一个 backend 实例 + 不断切换的上下文，才是真实形态。
    ``_allowed()`` 走 ``get_runtime().context``（图执行期才有），直接调时用假 runtime 顶替。
    """
    fanout.get_runtime = lambda: SimpleNamespace(
        context=AgentContext(
            user_id=user_id,
            memory_workspaces=ALICE_SPACES if spaces is None else spaces,
        )
    )
    return backend


def paths(result) -> list[str]:  # noqa: ANN001
    return [entry["path"] for entry in (result.entries or [])]


def keys(user_id: str, workspace_id: str) -> list[str]:
    return [item.key for item in store.search((user_id, workspace_id, "filesystem"))]


def main() -> None:
    print("== 0. 准备：proj-a 那格放一份资料（相当于用户走 /memories/upload 投喂）==")
    store.put(("alice", "wsa", "filesystem"), "/notes.md", create_file_data("proj-a 的资料"))
    print(f"  proj-a -> {keys('alice', 'wsa')}")

    print("== 1. 清单：只列可见格子，空空间也列 ==")
    # 每次操作前都重新 use(...)：backend 是单例，上下文是每轮一件的，别拿旧变量当真
    alice = use()
    assert paths(alice.ls("/")) == ["/default/", "/proj-a/", "/proj-b/"], paths(alice.ls("/"))
    print(f"  ls /memories/ -> {paths(alice.ls('/'))}")
    assert alice.ls("/proj-b").error is None, "空格子也进得去（列出来是空目录）"
    assert paths(use(spaces={"default": "default"}).ls("/")) == ["/default/"]
    assert use(spaces={}).ls("/").entries == [], "拿不到清单时不该列出任何格子"

    print("== 2. 隔离：命名空间是 (user_id, 空间)，换个人读同一格读不到 ==")
    got = use().read("/proj-a/notes.md")
    print(f"  alice 读 /proj-a/notes.md -> {got.file_data['content']!r}")
    assert got.error is None and got.file_data["content"] == "proj-a 的资料"
    denied = use(user_id="bob").read("/proj-a/notes.md")
    print(f"  bob 读同一路径 -> error={denied.error!r}")
    assert denied.error is not None, "不同用户命中了同一份文件"
    assert keys("bob", "wsa") == [], "bob 的命名空间不该有数据"

    print("== 3. 写：只有 default 那一格放行，其余一律拒 ==")
    alice = use()
    assert alice.write("/default/prefs.md", "喜欢简短回答").error is None
    for target in ("/proj-a/hack.md", "/proj-b/x.md", "/memories/x.md", "/"):
        assert alice.write(target, "x").error == MEMORY_DENIED, target
        assert alice.edit(target, "a", "b").error == MEMORY_DENIED, target
        assert alice.delete(target).error == MEMORY_DENIED, target
    print(f"  proj-a 被拒后仍是 {keys('alice', 'wsa')}；default -> {keys('alice', 'default')}")
    assert keys("alice", "wsa") == ["/notes.md"], "被拒的写入不该落库"
    assert keys("alice", "default") == ["/prefs.md"]

    print("== 4. 静态规则：default 格 interrupt，其余 deny（读一律放行）==")
    for target, mode in (
        (f"{MEMORY_ROUTE}default/prefs.md", "interrupt"),
        (f"{MEMORY_ROUTE}default", "interrupt"),
        (f"{MEMORY_ROUTE}proj-a/notes.md", "deny"),
        (f"{MEMORY_ROUTE}proj-a", "deny"),
        (MEMORY_ROUTE.rstrip("/"), "deny"),
    ):
        actual = _check_fs_permission(MEMORY_PERMISSIONS, "write", target)
        assert actual == mode, (target, actual)
    print("  default=interrupt 其它=deny 裸 '/memories'=deny")
    assert (
        _check_fs_permission(
            MEMORY_PERMISSIONS, "read", f"{MEMORY_ROUTE}proj-a/notes.md"
        )
        == "allow"
    )

    print("== 5. 扇出：挂载根的 glob / grep 一次搜多个格子，命中带格子名 ==")
    found = sorted(m["path"] for m in (use().glob("*.md").matches or []))
    print(f"  glob '*.md' -> {found}")
    assert found == ["/default/prefs.md", "/proj-a/notes.md"], found
    hits = sorted(m["path"] for m in (use().grep("资料").matches or []))
    assert hits == ["/proj-a/notes.md"], hits
    one = use().glob("*.md", "/proj-a")
    assert [m["path"] for m in (one.matches or [])] == ["/proj-a/notes.md"], one
    # 看不见的格子：既不列也不搜
    assert use(spaces={"default": "default"}).glob("*.md", "/proj-a").matches == []

    print("== 6. 端到端：假模型真的调 ls / read_file / write_file ==")
    fanout.get_runtime = REAL_GET_RUNTIME  # 图执行期用真的 runtime
    agent = create_deep_agent(
        model=FakeToolModel(
            responses=[
                AIMessage("", tool_calls=[{"name": "ls", "args": {"path": "/memories/"}, "id": "c1"}]),
                AIMessage("", tool_calls=[{"name": "read_file", "args": {"file_path": "/memories/proj-a/notes.md"}, "id": "c2"}]),
                AIMessage("", tool_calls=[{"name": "write_file", "args": {"file_path": "/memories/proj-a/hack.md", "content": "x"}, "id": "c3"}]),
                AIMessage("", tool_calls=[{"name": "write_file", "args": {"file_path": "/memories/default/remembered.md", "content": "记得"}, "id": "c4"}]),
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
    context = AgentContext(user_id="alice", memory_workspaces=ALICE_SPACES)
    cfg = {"configurable": {"thread_id": "alice:mine:t1"}}
    out = agent.invoke(
        {"messages": [{"role": "user", "content": "看看我的记忆"}]}, config=cfg, context=context
    )
    tools = [m.content for m in out["messages"] if m.type == "tool"]
    for content in tools:
        print(f"  -> {content.splitlines()[0][:90]}")
    assert len(tools) == 3, tools  # 第 4 个（写 default）被拦下等人批准，还没有工具结果
    assert "default" in tools[0] and "proj-a" in tools[0], tools[0]
    assert "proj-a 的资料" in tools[1], tools[1]
    assert "permission denied" in tools[2], tools[2]
    assert keys("alice", "wsa") == ["/notes.md"], "被拒的写入不该落库"

    step_4 = (out.get("__interrupt__") or [None])[0]
    assert step_4 is not None, f"没触发人工审批：{out.keys()}"
    action = step_4.value["action_requests"][0]
    print(f"  待批准：{action['name']} {action['args']}")
    assert action["args"]["file_path"] == "/memories/default/remembered.md", action
    assert keys("alice", "default") == ["/prefs.md"], "批准前不该落库"

    out2 = agent.invoke(
        Command(resume={"decisions": [{"type": "approve"}]}), config=cfg, context=context
    )
    print(f"  批准后 default -> {keys('alice', 'default')}，回答={out2['messages'][-1].content!r}")
    assert sorted(keys("alice", "default")) == ["/prefs.md", "/remembered.md"]

    print("== 7. 批量下载：按路径选格子，命中路径补回格子名；看不见的一律 file_not_found ==")
    downloader = use()
    wanted = ["/proj-a/notes.md", "/proj-b/nope.md", "/missing/x.md", "/"]
    responses = downloader.download_files(wanted)
    for response in responses:
        print(
            f"  {response.path:22} -> error={response.error!r} bytes={response.content!r}"
        )
    assert [r.path for r in responses] == wanted, "顺序和入参一致"
    assert responses[0].content == "proj-a 的资料".encode(), responses[0]
    assert responses[0].error is None
    for missing in responses[1:]:
        assert missing.content is None and missing.error == "file_not_found", missing
    import asyncio

    assert asyncio.run(downloader.adownload_files(wanted)) == responses, "异步版应与同步版一致"

    print("== 8. 批量上传一律拒（它是写，却绕开人工批准）==")
    uploaded = downloader.upload_files([("/default/sneak.md", b"x")])
    print(f"  upload -> {uploaded}")
    assert [(r.path, r.error) for r in uploaded] == [
        ("/default/sneak.md", "permission_denied")
    ], uploaded
    assert keys("alice", "default") == ["/prefs.md", "/remembered.md"], "批量上传不该落库"

    print("\n全部断言通过。")


if __name__ == "__main__":
    main()
