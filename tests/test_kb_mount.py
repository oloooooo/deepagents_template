"""验证知识库挂载 ``/kb/``：一格一微服务，格内 shared / private 两层（docs/adr/0003）。

不需要数据库、不需要模型 API：``InMemoryStore`` + 假模型。

1) 可见清单：格子来自当轮 ``AgentContext.kb_cells``，空上下文 = 什么都看不见；
2) 格子结构：``ls /{微服务名}/`` 恒为 ``shared/`` + ``private/`` 两层；
3) shared 可见即读得到，private 只有自己那份（换个人同路径读不到）；
4) 写一律拒（同步 + 异步），批量上传拒；静态规则 ``/kb`` 与 ``/kb/**`` 都是 deny、读放行；
5) 扇出：根上 ``glob`` 覆盖所有可见格子的两层，路径自带格子名前缀；不存在的格子/层 404 口径；
6) 端到端：假模型真的 ls / read_file，还尝试 write_file —— 被拒且**不触发**人工批准。

运行方式：``uv run python tests/test_kb_mount.py``
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
from langgraph.types import Command  # noqa: E402

import agents.kb.mount as kb_mount_mod  # noqa: E402
from agents.agent import MEMORY_PERMISSIONS, MEMORY_ROUTE, AgentContext, memory_mount  # noqa: E402
from agents.kb import KB_PERMISSIONS, KB_ROUTE, kb_mount  # noqa: E402
from agents.kb.storage import aput_bytes  # noqa: E402

MS1, MS1_ID, MS2, MS2_ID = "order-svc", "id_ms1", "pay-svc", "id_ms2"
store = InMemoryStore()
backend = kb_mount(store)

REAL_GET_RUNTIME = kb_mount_mod.get_runtime


class FakeToolModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return self


def use(user_id: str = "alice", cells: dict[str, str] | None = None):
    """切到"当轮上下文"并返回挂载 backend（一个实例 + 不断切换的上下文 = 真实形态）。"""
    kb_mount_mod.get_runtime = lambda: SimpleNamespace(
        context=AgentContext(
            user_id=user_id, kb_cells=cells if cells is not None else {MS1: MS1_ID}
        )
    )
    return backend


def paths(result) -> list[str]:  # noqa: ANN001
    return [entry["path"] for entry in (result.entries or [])]


def ns_keys(namespace: tuple[str, ...]) -> list[str]:
    return sorted(item.key for item in store.search(namespace))


async def seed() -> None:
    await aput_bytes(
        name=MS1, microservice_id=MS1_ID, layer="shared",
        path="概念.md", content="订单=先扣库存".encode(), store=store,
    )
    await aput_bytes(
        name=MS1, microservice_id=MS1_ID, layer="private", user_id="alice",
        path="表.xlsx", content=b"\xff\xfePK\x03\x04alice-bin", store=store,
    )
    await aput_bytes(
        name=MS2, microservice_id=MS2_ID, layer="shared",
        path="支付.md", content="支付=回调".encode(), store=store,
    )


def main() -> None:
    import asyncio

    asyncio.run(seed())

    print("== 1. 可见清单来自 kb_cells，空上下文什么都看不见 ==")
    alice = use()
    assert paths(alice.ls("/")) == [f"/{MS1}/"], paths(alice.ls("/"))
    print(f"  alice ls /kb/ -> {paths(alice.ls('/'))}")
    nobody = use(cells={})
    assert paths(nobody.ls("/")) == [], "没配可见清单时不该列任何格子"
    print("  kb_cells={} -> []（不是报错，也不是全量）")
    stranger = use(user_id="mallory", cells=None)
    assert paths(stranger.ls("/")) == [f"/{MS1}/"], "可见清单与是谁无关（由 REST 侧把关）"

    print("== 2. 格子结构：恒为 shared/ + private/ ==")
    assert paths(alice.ls(f"/{MS1}")) == ["/shared/", "/private/"]
    print(f"  ls /{MS1}/ -> {paths(alice.ls('/' + MS1))}")

    print("== 3. shared 全员可读，private 只有自己那份 ==")
    alice = use()  # 上一步把上下文切到别人了，换回 alice
    got = alice.read(f"/{MS1}/shared/概念.md")
    assert got.error is None and "库存" in got.file_data["content"], got.error
    print(f"  alice 读 shared/概念.md -> {got.file_data['content']!r}")
    mine = alice.read(f"/{MS1}/private/表.xlsx")
    assert mine.error is None and mine.file_data["encoding"] == "base64", mine.error
    print("  alice 读自己的 private/表.xlsx -> base64 原样存取（不解析，Q9=a）")
    # bob 有同一格的可见权，但 private 命名空间是按 user_id 分的
    bob = use(user_id="bob")
    assert paths(bob.ls(f"/{MS1}/private")) == [], "bob 看不到 alice 的私有文件"
    assert bob.read(f"/{MS1}/private/表.xlsx").error is not None, "bob 读到了别人的私有文件"
    print("  bob 读同一路径 -> 不存在（private 按 user_id 分命名空间）")

    print("== 4. 写一律拒（同步 + 异步 + 批量），静态规则读放行 ==")
    alice = use()
    assert "只读" in alice.write(f"/{MS1}/shared/hack.md", "x").error
    assert "只读" in alice.delete(f"/{MS1}/shared/概念.md").error
    denied = asyncio.run(alice.awrite(f"/{MS1}/shared/hack.md", "x"))
    assert "只读" in denied.error, denied
    uploaded = alice.upload_files([("/sneak.md", b"x")])
    assert [(r.path, r.error) for r in uploaded] == [("/sneak.md", "permission_denied")]
    print("  write / delete / awrite -> 只读拒绝；upload -> permission_denied")
    for target in (f"{KB_ROUTE}shared/x.md", KB_ROUTE.rstrip("/")):
        assert _check_fs_permission(KB_PERMISSIONS, "write", target) == "deny", target
    assert _check_fs_permission(KB_PERMISSIONS, "read", f"{KB_ROUTE}a/s/b.md") == "allow"
    print("  裸 '/kb' 与 '/kb/**' write=deny，读放行")
    # 底层内容没被动过
    assert ns_keys(("kb", MS1_ID, "shared")) == ["/概念.md"], ns_keys(("kb", MS1_ID, "shared"))
    print("  底层 shared 命名空间纹丝不动")

    print("== 5. 扇出：根上 glob 覆盖所有可见格子的两层，带格子名前缀 ==")
    alice = use()
    result = alice.glob("**")
    found = sorted(m["path"] for m in (result.matches or []))
    assert f"/{MS1}/shared/概念.md" in found, found
    assert f"/{MS1}/private/表.xlsx" in found, found
    assert f"/{MS2}/shared/支付.md" not in found, "越权扇出了不可见的格子"
    print(f"  alice glob ** -> {found}")
    bob_all = use(user_id="bob", cells={MS1: MS1_ID, MS2: MS2_ID}).glob("**")
    found2 = sorted(m["path"] for m in (bob_all.matches or []))
    assert f"/{MS2}/shared/支付.md" in found2, found2
    print(f"  可见两个格子时 -> {found2}")
    alice = use()  # 切回 alice 再断言越权读
    assert alice.read(f"/{MS2}/shared/支付.md").error is not None, "读到了不可见格子"
    assert alice.read(f"/{MS1}/bogus/概念.md").error is not None, "不存在的层竟然读到了"
    print("  不可见格子 / 不存在的层 -> 一律「不存在」")

    print("== 6. 端到端：假模型 ls + read + 试写（被拒且不触发人工批准）==")
    kb_mount_mod.get_runtime = REAL_GET_RUNTIME
    agent = create_deep_agent(
        model=FakeToolModel(
            responses=[
                AIMessage("", tool_calls=[{"name": "ls", "args": {"path": "/kb/"}, "id": "k1"}]),
                AIMessage("", tool_calls=[{"name": "read_file", "args": {"file_path": f"/kb/{MS1}/shared/概念.md"}, "id": "k2"}]),
                AIMessage("", tool_calls=[{"name": "write_file", "args": {"file_path": f"/kb/{MS1}/shared/hack.md", "content": "改一下"}, "id": "k3"}]),
                AIMessage("done"),
            ]
        ),
        backend=CompositeBackend(
            default=StateBackend(),
            routes={MEMORY_ROUTE: memory_mount(store), KB_ROUTE: kb_mount(store)},
        ),
        permissions=MEMORY_PERMISSIONS + KB_PERMISSIONS,
        checkpointer=InMemorySaver(),
        context_schema=AgentContext,
    )
    context = AgentContext(user_id="alice", kb_cells={MS1: MS1_ID})
    out = agent.invoke(
        {"messages": [{"role": "user", "content": "看看订单知识库"}]},
        config={"configurable": {"thread_id": "alice:kb:t1"}},
        context=context,
    )
    tools = [m.content for m in out["messages"] if m.type == "tool"]
    for content in tools:
        print(f"  -> {content.splitlines()[0][:90]}")
    assert len(tools) == 3, tools  # 三个工具都执行了：写被 deny（不是 interrupt）
    assert f"/{MS1}" in tools[0], tools[0]
    assert "库存" in tools[1], tools[1]
    assert "只读" in tools[2] or "permission" in tools[2].lower(), tools[2]
    assert not out.get("__interrupt__"), "知识库写入不该触发人工批准（deny 不是 interrupt）"
    assert ns_keys(("kb", MS1_ID, "shared")) == ["/概念.md"], "尝试的写入竟然落库了"
    print("  写被静态 deny 挡下，未触发 interrupt，底层无变化")

    print("\n全部断言通过。")


if __name__ == "__main__":
    main()
