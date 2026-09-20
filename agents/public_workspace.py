"""公共空间：agent 侧的只读挂载 + store 侧的读写。

两件事放在同一个模块，因为它们共用同一个命名空间定义（:func:`public_namespace`）：

- :class:`PublicMountBackend` —— agent 看到的 ``/public/{公共空间名}/...``，**只读**，
  可见范围来自当轮的 ``AgentContext.public_workspaces``；
- :class:`PublicWorkspaceStore` —— REST 侧（super 写）和删除清理用的 store 读写。

命名空间 **不含 user_id**（``("public", <id>, "filesystem")``）—— 这就是"公共"的定义，
也是它和 ``/memories/``（``(user_id, workspace_id, "filesystem")``）的根本区别。

为什么挂载要自己写一个 backend（而不是直接用 ``StoreBackend``）：``CompositeBackend`` 的路由是
**启动时定死的静态前缀**，没法按请求塞进"这个人能看哪几个公共空间"；而 ``StoreBackend`` 一个实例
只有一个命名空间，所有用户读同一个命名空间就看得见**全部**公共空间。所以需要一个按当轮
``rt.context`` 过滤、再委托给各空间自己的 ``StoreBackend`` 的中间层。
"""

from typing import Any

from deepagents import FilesystemPermission
from deepagents.backends import StoreBackend
from deepagents.backends.protocol import (
    DeleteResult,
    EditResult,
    FileInfo,
    GlobResult,
    GrepMatch,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.utils import create_file_data, validate_path
from langgraph.runtime import get_runtime
from langgraph.store.postgres.aio import AsyncPostgresStore

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


def public_namespace(public_workspace_id: str) -> tuple[str, str, str]:
    """公共空间内容的 store 命名空间。

    不含 ``user_id``：同一个公共空间的成员读的是**同一份**内容。
    """
    return ("public", public_workspace_id, "filesystem")


class PublicMountBackend(StoreBackend):
    """``/public/{公共空间名}/...`` 的只读挂载。

    ``CompositeBackend`` 已经把路由前缀 ``/public/`` 剥掉了，所以这里收到的路径形如
    ``/{公共空间名}/{空间内路径}``（挂载根是 ``/``）。

    **可见范围只来自当轮的 ``AgentContext.public_workspaces``（名字 -> id）**，不查库：
    backend 是同步的，拿不到数据库 session。super 的可见范围是"全部"，由 dependency 在
    每轮填充时体现（``services/public_workspace.py``），所以这里不需要判 ``is_super``。

    写方法一律返回错误。middleware 的静态 deny 规则（``PUBLIC_PERMISSIONS``）本该先一步挡掉，
    这里是第二道防线 —— 规则写漏时宁可直接报错，也不能静默写进共享空间。
    """

    def __init__(self, store: AsyncPostgresStore) -> None:
        # namespace 是占位值：每个方法都按路径换到对应空间的 backend，从不直接用 self 的
        super().__init__(
            namespace=lambda _rt: ("public", "unset", "filesystem"), store=store
        )
        self._cache: dict[str, StoreBackend] = {}

    # ---------- 路径与可见范围 ----------

    @staticmethod
    def _split(path: str) -> tuple[str, str] | None:
        """``/{名}/{空间内路径}`` -> ``(名, /空间内路径)``；挂载根（无名字段）返回 ``None``。"""
        name, _, rest = path.strip("/").partition("/")
        return (name, f"/{rest}") if name else None

    @staticmethod
    def _allowed() -> dict[str, str]:
        """当轮可见的公共空间（名字 -> id）。拿不到运行期上下文时返回空（什么都不给看）。"""
        try:
            context: Any = get_runtime().context
        except (RuntimeError, KeyError):
            return {}
        return dict(getattr(context, "public_workspaces", None) or {})

    def _backend(self, name: str) -> StoreBackend | None:
        """名字 -> 该空间自己的 ``StoreBackend``（按 id 分命名空间，实例缓存复用）。"""
        workspace_id = self._allowed().get(name)
        if workspace_id is None:
            return None
        if workspace_id not in self._cache:
            self._cache[workspace_id] = StoreBackend(
                namespace=lambda _rt, _id=workspace_id: public_namespace(_id),
                store=self._store,
            )
        return self._cache[workspace_id]

    def _fanout(self, path: str | None) -> list[tuple[str, StoreBackend, str]]:
        """把一次搜索展开成 ``[(名字, backend, 空间内路径), ...]``。

        路径落在某个公共空间里就只展开那一个；落在挂载根（或没给路径）就展开全部可见的。
        """
        split = self._split(path) if path else None
        if split is not None:
            backend = self._backend(split[0])
            return [(split[0], backend, split[1])] if backend is not None else []
        return [
            (name, backend, "/")
            for name in sorted(self._allowed())
            if (backend := self._backend(name)) is not None
        ]

    # ---------- 读 ----------

    def ls(self, path: str) -> LsResult:
        if self._split(path) is None:
            # 挂载根：把可见的公共空间列成目录
            return LsResult(
                entries=[
                    FileInfo(path=f"/{name}/", is_dir=True, size=0, modified_at="")
                    for name in sorted(self._allowed())
                ]
            )
        name, inner = self._split(path)  # type: ignore[misc]
        backend = self._backend(name)
        if backend is None:
            return LsResult(error=f"目录 '{path}' 不存在")
        return backend.ls(inner)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        split = self._split(file_path)
        if split is None:
            return ReadResult(error=f"'{file_path}' 是目录，不是文件")
        backend = self._backend(split[0])
        if backend is None:
            return ReadResult(error=f"文件 '{file_path}' 不存在")
        return backend.read(split[1], offset, limit)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        matches: list[FileInfo] = []
        truncated = False
        for name, backend, inner in self._fanout(path):
            result = backend.glob(pattern, inner)
            if result.error:
                return result
            truncated = truncated or result.truncated
            matches.extend(
                {**match, "path": f"/{name}{match['path']}"}
                for match in (result.matches or [])
            )
        return GlobResult(matches=matches, truncated=truncated)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        matches: list[GrepMatch] = []
        truncated = False
        for name, backend, inner in self._fanout(path):
            result = backend.grep(pattern, inner, glob, max_count=max_count)
            if result.error:
                return result
            truncated = truncated or result.truncated
            matches.extend(
                {**match, "path": f"/{name}{match['path']}"}
                for match in (result.matches or [])
            )
        return GrepResult(matches=matches, truncated=truncated)

    # ---------- 写：全部拒绝（见类 docstring） ----------

    DENIED = "公共空间对 agent 只读：只有 super 用户能通过 API 修改公共内容"

    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=self.DENIED)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return EditResult(error=self.DENIED)

    def delete(self, file_path: str) -> DeleteResult:
        return DeleteResult(error=self.DENIED)


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
