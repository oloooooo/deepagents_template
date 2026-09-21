"""多命名空间挂载：``/{挂载根}/{格子名}/...``，一个格子一个 store 命名空间。

两个挂载共用它（别各写一份）：

======================  =================  =====================================  ==========
挂载点                   格子名              格子对应的 store 命名空间               可写的格子
======================  =================  =====================================  ==========
``/public/``            公共空间名          ``("public", id, "filesystem")``        无（只读）
``/memories/``          业务空间名          ``(user_id, id, "filesystem")``         只有 default
======================  =================  =====================================  ==========

**为什么要自己写一层**：``CompositeBackend`` 的路由是**启动时定死的静态前缀**，没法按请求决定
"这个人能看哪几个格子"；而一个 ``StoreBackend`` 只有一个命名空间（所有用户读同一个命名空间就
看得见**全部**格子）。所以中间需要一层：按当轮 ``rt.context``（``AgentContext``）过滤出可见格子，
再委托给每个格子自己的 ``StoreBackend``。

两个挂载的差别只有三处，都做成构造参数：清单在 context 的哪个字段（``context_attr``）、
格子名怎么映射到命名空间（``namespace``）、哪一格可写（``writable``，``None`` = 全只读）。

**格子名与 id 不是一件事**：REST 侧按 id 寻址，agent 侧按名字寻址，两者的取舍见
``docs/adr/0002``（公共空间名不可变）与 ``docs/adr/0009``（业务空间名可改）。

**写**：静态权限规则（``PUBLIC_PERMISSIONS`` / ``MEMORY_PERMISSIONS``）本该先一步挡住，这里是
第二道防线 —— 规则写漏时宁可直接报错，也不能静默写进不该写的地方。
"""

import base64
from collections.abc import Callable
from typing import Any

from deepagents.backends import StoreBackend
from deepagents.backends.protocol import (
    DeleteResult,
    EditResult,
    FileDownloadResponse,
    FileUploadResponse,
    FileInfo,
    GlobResult,
    GrepMatch,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.utils import file_data_to_string
from langgraph.runtime import get_runtime
from langgraph.store.postgres.aio import AsyncPostgresStore

__all__ = ["FanoutMountBackend"]

NamespaceFactory = Callable[[Any, str], tuple[str, ...]]
"""``(当轮 context, 格子 id) -> store 命名空间``。"""


def _unreachable(_rt: Any) -> tuple[str, ...]:
    """占位命名空间：只会在某个方法没被本类接管时被调到 —— 那就直接炸，别静默读写一个不存在的地方。"""
    raise NotImplementedError("FanoutMountBackend 漏了接管的方法（它的命名空间是逐路径算的）")


class FanoutMountBackend(StoreBackend):
    """把一个挂载根分发到多个 store 命名空间上，每个命名空间一个子目录。

    ``CompositeBackend`` 已经把挂载前缀剥掉了，所以这里收到的路径形如
    ``/{格子名}/{格子内路径}``（挂载根是 ``/``，裸 ``/`` 就是格子清单本身）。

    接管了 ``FilesystemMiddleware`` 会调的那几个方法（``ls`` / ``read`` / ``glob`` /
    ``grep`` / ``write`` / ``edit`` / ``delete`` 及其 ``a*`` 版）——**同步和异步必须成对覆盖**：
    ``StoreBackend`` 只有读的 ``als`` / ``aglob`` / ``agrep`` 是转调同步版，
    ``aread`` / ``awrite`` / ``aedit`` / ``adelete`` 是另一套原生实现，不覆盖就会绕开这里的写策略。
    另外接了批量下载（``download_files`` / ``adownload_files``，按路径选格子）与
    批量上传（一律拒 —— 它是写，却绕开人工批准，见下）。
    """

    def __init__(
        self,
        store: AsyncPostgresStore,
        *,
        context_attr: str,
        namespace: NamespaceFactory,
        writable: str | None = None,
        denied: str = "这个挂载点对 agent 只读",
    ) -> None:
        super().__init__(namespace=_unreachable, store=store)
        self._context_attr = context_attr
        self._namespace_of = namespace
        self._writable = writable
        self._denied = denied

    # ---------- 当轮可见范围 ----------

    @staticmethod
    def _context() -> Any:
        """当轮运行期上下文；图外直接调（或没传 context）时返回 ``None``。"""
        try:
            return get_runtime().context
        except (RuntimeError, KeyError):
            return None

    def _allowed(self) -> dict[str, str]:
        """当轮可见的格子（名字 -> id）。拿不到上下文时返回空：什么都不给看。"""
        context = self._context()
        if context is None:
            return {}
        return dict(getattr(context, self._context_attr, None) or {})

    def _backend(self, name: str) -> StoreBackend | None:
        """格子名 -> 该格子自己的 ``StoreBackend``（按命名空间建；它只是个两字段的壳，不缓存）。"""
        context = self._context()
        workspace_id = self._allowed().get(name)
        if context is None or workspace_id is None:
            return None
        scoped = self._namespace_of(context, workspace_id)
        return StoreBackend(namespace=lambda _rt, _ns=scoped: _ns, store=self._store)

    # ---------- 路径 ----------

    @staticmethod
    def _split(path: str) -> tuple[str, str] | None:
        """``/{格子名}/{格子内路径}`` -> ``(格子名, /格子内路径)``；挂载根返回 ``None``。"""
        name, _, rest = path.strip("/").partition("/")
        return (name, f"/{rest}") if name else None

    def _fanout(self, path: str | None) -> list[tuple[str, StoreBackend, str]]:
        """把一次搜索展开成 ``[(格子名, backend, 格子内路径), ...]``。

        路径落在某个格子里就只展开那一个；落在挂载根（或没给路径）就展开全部可见的。
        ``max_count`` 是**逐格子**生效的，所以 N 个格子最多返回 N 倍命中。
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
            # 挂载根：把可见的格子列成目录（清单来自可见范围，不看内容 —— 空格子也列出来）
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

    # ---------- 写：只有 writable 那一格放行 ----------

    def _writable_target(self, path: str) -> tuple[StoreBackend, str] | None:
        """路径落在可写的那一格时返回 ``(它的 backend, 格子内路径)``，否则 ``None``。"""
        if self._writable is None:
            return None
        split = self._split(path)
        if split is None or split[0] != self._writable:
            return None
        backend = self._backend(split[0])
        return (backend, split[1]) if backend is not None else None

    def write(self, file_path: str, content: str) -> WriteResult:
        target = self._writable_target(file_path)
        if target is None:
            return WriteResult(error=self._denied)
        backend, inner = target
        return backend.write(inner, content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        target = self._writable_target(file_path)
        if target is None:
            return EditResult(error=self._denied)
        backend, inner = target
        return backend.edit(inner, old_string, new_string, replace_all)

    def delete(self, file_path: str) -> DeleteResult:
        target = self._writable_target(file_path)
        if target is None:
            return DeleteResult(error=self._denied)
        backend, inner = target
        return backend.delete(inner)

    # ---------- 同一个策略的异步版（中间件走的是这几个） ----------

    async def aread(
        self, file_path: str, offset: int = 0, limit: int = 2000
    ) -> ReadResult:
        split = self._split(file_path)
        if split is None:
            return ReadResult(error=f"'{file_path}' 是目录，不是文件")
        backend = self._backend(split[0])
        if backend is None:
            return ReadResult(error=f"文件 '{file_path}' 不存在")
        return await backend.aread(split[1], offset, limit)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        target = self._writable_target(file_path)
        if target is None:
            return WriteResult(error=self._denied)
        backend, inner = target
        return await backend.awrite(inner, content)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        target = self._writable_target(file_path)
        if target is None:
            return EditResult(error=self._denied)
        backend, inner = target
        return await backend.aedit(inner, old_string, new_string, replace_all)

    async def adelete(self, file_path: str) -> DeleteResult:
        target = self._writable_target(file_path)
        if target is None:
            return DeleteResult(error=self._denied)
        backend, inner = target
        return await backend.adelete(inner)

    # 批量下载：按路径选格子，命中路径补回格子名前缀。读不走写策略，所以整棵树都能下。
    #
    # 不复用 ``StoreBackend.download_files``：它在这套壳下拿不准（对 AsyncPostgresStore
    # 实测会把存在的文件报成 file_not_found），所以照 ``read`` / ``aread`` 自己拼一段：
    # 一个格子的读怎么走，下载就怎么走。失败一律 ``file_not_found`` —— 与 read 同一口径，
    # 不区分“不存在”和“看不见”，免得被拿来做存在性探测。
    @staticmethod
    def _downloaded(path: str, result: ReadResult) -> FileDownloadResponse:
        """``ReadResult`` -> ``FileDownloadResponse``（bytes 按 ``encoding`` 还原）。"""
        data = result.file_data
        if result.error or data is None:
            return FileDownloadResponse(path=path, content=None, error="file_not_found")
        text = file_data_to_string(data)
        content = (
            base64.standard_b64decode(text)
            if data.get("encoding") == "base64"
            else text.encode("utf-8")
        )
        return FileDownloadResponse(path=path, content=content, error=None)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """批量读原始内容。

        给中间件用的（``read`` 返回的是带行号的文本，它们要的是原始内容）；模型的文件工具不走这里
        （读文件是 ``read_file`` → ``aread``）。
        """
        return [self._downloaded(path, self.read(path)) for path in paths]

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """`download_files` 的异步版（中间件走的是这个）。"""
        return [self._downloaded(path, await self.aread(path)) for path in paths]

    # 批量上传**一律拒**：它是写，但走的是中间件的内部调用，不经文件工具，
    # 也就绕开了 MEMORY_PERMISSIONS 的人工批准 —— 宁可让“写记忆要批准”这条规矩没有例外。
    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [
            FileUploadResponse(path=path, error="permission_denied") for path, _ in files
        ]

    async def aupload_files(
        self, files: list[tuple[str, bytes]]
    ) -> list[FileUploadResponse]:
        return self.upload_files(files)
