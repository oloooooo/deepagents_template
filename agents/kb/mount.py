"""``/kb/`` 挂载：一个微服务一格，格内 ``shared`` / ``private`` 两层。

路径形如（``CompositeBackend`` 已剥掉 ``/kb/`` 前缀，这里收到的是内层路径）::

    /                        可见微服务清单（名字 -> 格子，来自当轮 AgentContext.kb_cells）
    /{微服务名}/             ["shared/", "private/"]
    /{微服务名}/shared/...   共享知识（概念.md）
    /{微服务名}/private/...  **当前用户自己**的私有文档（super 看别人的私有走 REST）

**为什么要自己写一层**：``CompositeBackend`` 的路由是启动时定死的静态前缀；而一个格子的
backend 取决于（当轮可见清单、层、配置选的介质），只能按请求算（与旧 fanout 同一个理由，
实现重写——旧代码在 ``16c3cf9^`` 可参考）。

只读的两道防线（docs/adr/0003）：middleware 层有 ``KB_PERMISSIONS`` 的静态 deny；
这里的写方法照样返回错误——规则写漏时宁可直接报错，也不能静默写进不该写的地方。
批量上传同理一律拒（它是写，还绕开文件工具那层的规则）。

**同步与异步成对**：中间件走的可能是任一侧；store 的同步读在事件循环线程里会
``run_coroutine_threadsafe`` 卡死自己（异步客户端只有原生异步与 to_thread 两条活路），
所以同步方法只作为协议兜底，异步方法一律委托 cell 的异步原生实现。
"""

import base64
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

from agents.kb.storage import KB_LAYERS, cell_backend

__all__ = ["KbMountBackend", "kb_mount"]

DENIED = "知识库对 agent 只读，写入走 /kb/files/* REST 接口"


def _unreachable(_rt: Any) -> tuple[str, ...]:
    """占位命名空间：cell 的命名空间是逐路径算的，走到这就说明漏了接管，直接炸。"""
    raise NotImplementedError("KbMountBackend 漏了接管的方法（它的命名空间是逐路径算的）")


class KbMountBackend(StoreBackend):
    """把 ``/kb/`` 分发到「格子 × 层」自己的 backend 上（store 或 local，见 storage）。"""

    def __init__(self, store: Any) -> None:
        super().__init__(namespace=_unreachable, store=store)

    # ---------- 当轮可见范围 ----------

    @staticmethod
    def _context() -> Any:
        """当轮运行期上下文；图外直接调（或没传 context）时返回 ``None``。"""
        try:
            return get_runtime().context
        except (RuntimeError, KeyError):
            return None

    def _cells(self) -> dict[str, str]:
        """当轮可见的微服务（名字 -> id）。拿不到上下文时为空：什么都不给看。"""
        context = self._context()
        if context is None:
            return {}
        return dict(getattr(context, "kb_cells", None) or {})

    def _user_id(self) -> str | None:
        context = self._context()
        return getattr(context, "user_id", None) if context is not None else None

    def _cell(self, name: str, layer: str | None) -> Any | None:
        """(格子, 层) -> 该层自己的 backend；不可见 / 层不对 -> ``None``。"""
        if layer not in KB_LAYERS:
            return None
        microservice_id = self._cells().get(name)
        user_id = self._user_id()
        if microservice_id is None or user_id is None:
            return None
        return cell_backend(
            name=name,
            microservice_id=microservice_id,
            layer=layer,  # type: ignore[arg-type]
            user_id=user_id,
            store=self._store,
        )

    # ---------- 路径 ----------

    @staticmethod
    def _split(path: str) -> tuple[str, str | None, str] | None:
        """``/[{格子}/{层}[/...]]`` -> ``(格子, 层 | None, 层内路径)``；挂载根 -> ``None``。"""
        parts = path.strip("/").split("/")
        if not parts or parts == [""]:
            return None
        name = parts[0]
        if len(parts) == 1:
            return name, None, "/"
        layer = parts[1]  # 不是 shared/private 时 _cell 会拒
        rest = "/" + "/".join(parts[2:]) if len(parts) > 2 else "/"
        return name, layer, rest

    def _fan(self, path: str | None) -> list[tuple[str, str, Any, str]]:
        """把一次搜索展开成 ``[(格子, 层, backend, 层内路径), ...]``。

        路径落在某一层就只展开那一层；落在格子上就展开它的两层；落在挂载根就展开
        全部可见格子的两层。``max_count`` 是**逐 cell** 生效的，N 个 cell 最多 N 倍命中。
        """
        split = self._split(path) if path else None
        if split is None:
            names: list[tuple[str, str | None, str]] = [
                (name, None, "/") for name in sorted(self._cells())
            ]
        else:
            names = [split]
        fan: list[tuple[str, str, Any, str]] = []
        for name, layer, inner in names:
            layers = (layer,) if layer in KB_LAYERS else KB_LAYERS
            for each in layers:
                backend = self._cell(name, each)
                if backend is not None:
                    fan.append((name, each, backend, inner))
        return fan

    # ---------- 读 ----------

    def ls(self, path: str) -> LsResult:
        split = self._split(path)
        if split is None:
            # 挂载根：清单来自可见范围（空格子也列出来，不看内容）
            return LsResult(
                entries=[
                    FileInfo(path=f"/{name}/", is_dir=True, size=0, modified_at="")
                    for name in sorted(self._cells())
                ]
            )
        name, layer, rest = split
        if layer is None:
            # 格子层：两层是恒定结构，不用看内容
            if name not in self._cells():
                return LsResult(error=f"目录 '{path}' 不存在")
            return LsResult(
                entries=[
                    FileInfo(path=f"/{each}/", is_dir=True, size=0, modified_at="")
                    for each in KB_LAYERS
                ]
            )
        backend = self._cell(name, layer)
        if backend is None:
            return LsResult(error=f"目录 '{path}' 不存在")
        return backend.ls(rest)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        split = self._split(file_path)
        if split is None:
            return ReadResult(error=f"'{file_path}' 是目录，不是文件")
        name, layer, rest = split
        backend = self._cell(name, layer)
        if backend is None:
            return ReadResult(error=f"文件 '{file_path}' 不存在")
        return backend.read(rest, offset, limit)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        matches: list[FileInfo] = []
        truncated = False
        for name, layer, backend, inner in self._fan(path):
            result = backend.glob(pattern, inner)
            if result.error:
                return result
            truncated = truncated or result.truncated
            matches.extend(
                {**match, "path": f"/{name}/{layer}{match['path']}"}
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
        for name, layer, backend, inner in self._fan(path):
            result = backend.grep(pattern, inner, glob, max_count=max_count)
            if result.error:
                return result
            truncated = truncated or result.truncated
            matches.extend(
                {**match, "path": f"/{name}/{layer}{match['path']}"}
                for match in (result.matches or [])
            )
        return GrepResult(matches=matches, truncated=truncated)

    # ---------- 写：一律拒（静态 deny 之下的第二道防线） ----------

    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=DENIED)

    def edit(
        self, file_path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        return EditResult(error=DENIED)

    def delete(self, file_path: str) -> DeleteResult:
        return DeleteResult(error=DENIED)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=DENIED)

    async def aedit(
        self, file_path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        return EditResult(error=DENIED)

    async def adelete(self, file_path: str) -> DeleteResult:
        return DeleteResult(error=DENIED)

    # ---------- 同一个读策略的异步版（中间件优先走这些，原生异步，不落 to_thread） ----------

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        split = self._split(file_path)
        if split is None:
            return ReadResult(error=f"'{file_path}' 是目录，不是文件")
        name, layer, rest = split
        backend = self._cell(name, layer)
        if backend is None:
            return ReadResult(error=f"文件 '{file_path}' 不存在")
        return await backend.aread(rest, offset, limit)

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        matches: list[FileInfo] = []
        truncated = False
        for name, layer, backend, inner in self._fan(path):
            result = await backend.aglob(pattern, inner)
            if result.error:
                return result
            truncated = truncated or result.truncated
            matches.extend(
                {**match, "path": f"/{name}/{layer}{match['path']}"}
                for match in (result.matches or [])
            )
        return GlobResult(matches=matches, truncated=truncated)

    # ---------- 批量下载：读，放行；批量上传：写，拒 ----------

    @staticmethod
    def _downloaded(path: str, result: ReadResult) -> FileDownloadResponse:
        """``ReadResult`` -> ``FileDownloadResponse``（bytes 按 ``encoding`` 还原）。

        失败一律 ``file_not_found`` —— 与 read 同一口径，不区分「不存在」和「看不见」，
        免得被拿来做存在性探测。
        """
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
        return [self._downloaded(path, self.read(path)) for path in paths]

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return [self._downloaded(path, await self.aread(path)) for path in paths]

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [
            FileUploadResponse(path=path, error="permission_denied") for path, _ in files
        ]

    async def aupload_files(
        self, files: list[tuple[str, bytes]]
    ) -> list[FileUploadResponse]:
        return self.upload_files(files)


def kb_mount(store: Any) -> KbMountBackend:
    """``/kb/`` 挂载（写成函数是为了让测试建出**同一个**挂载，而不是照拄一份配置）。"""
    return KbMountBackend(store)
