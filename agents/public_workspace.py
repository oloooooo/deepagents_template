"""公共空间：agent 侧的只读挂载 + store 侧的读写。

两件事放在同一个模块，因为它们共用同一个命名空间定义（:func:`public_namespace`）：

- :class:`PublicMountBackend` —— agent 看到的 ``/public/{公共空间名}/...``，**只读**，
  可见范围来自当轮的 ``AgentContext.public_workspaces``。挂载的通用机制（扇出、只读策略）
  在 ``agents/fanout.py``，这里只剩配置：清单字段 + 命名空间 + "全只读"；
- :class:`PublicWorkspaceStore` —— REST 侧（super 写）和删除清理用的 store 读写。

命名空间 **不含 user_id**（``("public", <id>, "filesystem")``）—— 这就是"公共"的定义，
也是它和 ``/memories/``（``(user_id, workspace_id, "filesystem")``）的根本区别。
"""

from deepagents import FilesystemPermission
from deepagents.backends.utils import create_file_data, validate_path
from langgraph.store.postgres.aio import AsyncPostgresStore

from agents.fanout import FanoutMountBackend

__all__ = [
    "PUBLIC_PERMISSIONS",
    "PUBLIC_ROUTE",
    "PublicMountBackend",
    "PublicWorkspaceStore",
    "public_namespace",
]

PUBLIC_ROUTE = "/public/"

# agent 对 /public/** 一律拒绝写入：公共内容只有 super 能改，而 super 走 REST API。
# 静态规则区分不了用户（deny 会把 super 一起挡掉），所以"谁能写"根本不在 agent 这边表达 ——
# 见 docs/adr/0003。裸 "/public"（无尾斜杠）匹配不上 "/public/**"，单独列一条。
# operations 只有 "read" / "write" 两种，write_file / edit_file / delete 都算 "write"。
PUBLIC_PERMISSIONS = [
    FilesystemPermission(
        operations=["write"],
        paths=[f"{PUBLIC_ROUTE}**", PUBLIC_ROUTE.rstrip("/")],
        mode="deny",
    )
]

PUBLIC_DENIED = "公共空间对 agent 只读：只有 super 用户能通过 API 修改公共内容"


def public_namespace(public_workspace_id: str) -> tuple[str, str, str]:
    """公共空间内容的 store 命名空间。

    不含 ``user_id``：同一个公共空间的成员读的是**同一份**内容。
    """
    return ("public", public_workspace_id, "filesystem")


class PublicMountBackend(FanoutMountBackend):
    """``/public/{公共空间名}/...``：可见范围内每个公共空间一个子目录，全只读。

    ``CompositeBackend`` 已经把路由前缀 ``/public/`` 剥掉了，所以这里收到的路径形如
    ``/{公共空间名}/{空间内路径}``（挂载根是 ``/``）。

    **可见范围只来自当轮的 ``AgentContext.public_workspaces``（名字 -> id）**，不查库：
    backend 是同步的，拿不到数据库 session。super 的可见范围是"全部"，由 dependency 在
    每轮填充时体现（``services/public_workspace.py``），所以这里不需要判 ``is_super``。
    """

    def __init__(self, store: AsyncPostgresStore) -> None:
        super().__init__(
            store,
            context_attr="public_workspaces",
            namespace=lambda _context, workspace_id: public_namespace(workspace_id),
            denied=PUBLIC_DENIED,
        )


class PublicWorkspaceStore:
    """公共空间内容的读写：REST 侧（super 写）与删除清理都走它。

    和 ``AgentMemory`` 的区别只有两处：命名空间不含 ``user_id``（内容共享），
    路径也不带 ``/public/`` 前缀 —— 那是 agent 挂载点的前缀，REST 侧按 id 寻址。
    """

    def __init__(self, store: AsyncPostgresStore) -> None:
        self._store = store

    async def alist(self, public_workspace_id: str, *, limit: int = 200) -> list[str]:
        """该公共空间下的文件路径（最外层不带斜杠，如 ``notes/a.md``）。"""
        items = await self._store.asearch(
            public_namespace(public_workspace_id), limit=limit
        )
        return sorted(item.key.lstrip("/") for item in items)

    async def aread(self, public_workspace_id: str, path: str) -> str | None:
        """读一份内容，不存在返回 ``None``。"""
        item = await self._store.aget(
            public_namespace(public_workspace_id), _public_key(path)
        )
        content = item.value.get("content") if item else None
        return content if isinstance(content, str) else None

    async def awrite(self, public_workspace_id: str, path: str, content: str) -> str:
        """写 / 覆盖一份内容，返回规范化后的路径。"""
        key = _public_key(path)
        await self._store.aput(
            public_namespace(public_workspace_id), key, create_file_data(content)
        )
        return key.lstrip("/")

    async def adelete(self, public_workspace_id: str, path: str) -> bool:
        """删一份内容，不存在返回 ``False``。"""
        namespace = public_namespace(public_workspace_id)
        key = _public_key(path)
        if await self._store.aget(namespace, key) is None:
            return False
        await self._store.adelete(namespace, key)
        return True

    async def adelete_all(self, public_workspace_id: str, *, page: int = 100) -> int:
        """清空这个公共空间的全部内容，返回删掉的条数（删除公共空间时用）。

        ``BaseStore`` 没有 ``adelete_namespace``，只有 ``adelete(ns, key)`` / ``asearch``，
        所以只能翻页搜出来逐条删。每轮删掉搜到的那些，因此循环一定会结束。
        """
        namespace = public_namespace(public_workspace_id)
        deleted = 0
        while items := await self._store.asearch(namespace, limit=page):
            for item in items:
                await self._store.adelete(namespace, item.key)
                deleted += 1
        return deleted


def _public_key(path: str) -> str:
    """REST 侧传入的路径 -> store key（形如 ``/notes/a.md``）。

    和 ``AgentMemory`` 的路径校验同源：挡掉 ``..`` / ``~`` / 空路径，别把脏路径带进 store。
    """
    key = validate_path(f"/{path.strip().lstrip('/')}")
    if not key.strip("/"):
        raise ValueError(f"公共空间路径不能为空：{path!r}")
    return key
