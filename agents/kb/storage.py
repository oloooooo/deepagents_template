"""知识库存储后端：按 config 在 store / local 之间选，挂载层与 REST 共用（docs/adr/0002）。

一个微服务的库分两层（``layer``）：

- ``shared``  —— 共享知识（概念.md），全员只读，super 经 REST 写；
- ``private`` —— 某个用户的私有文档（xlsx），路径里再带一段 ``user_id``。

落点：

====================  =========================================  ==========================
layer                 store（langgraph store 命名空间）           local（磁盘目录）
====================  =========================================  ==========================
shared                ``("kb", 微服务 id, "shared")``             ``{local_root}/{微服务名}/shared``
private               ``("kb", 微服务 id, "private", 用户 id)``   ``{local_root}/{微服务名}/private/{用户 id}``
====================  =========================================  ==========================

选哪种由 ``config.yaml`` 的 ``kb.backend``（全局）+ ``kb.overrides``（按微服务名）决定；
``s3`` 枚举预留、未实现，取到直接 ``NotImplementedError``（不静默降级）。
"""

import base64
import shutil
from pathlib import Path, PurePosixPath
from typing import Literal

from deepagents.backends import FilesystemBackend, StoreBackend
from deepagents.backends.protocol import BackendProtocol
from deepagents.backends.utils import create_file_data
from langgraph.store.base import BaseStore

from config import app_config
from logger import logger

__all__ = [
    "KB_LAYER_PRIVATE",
    "KB_LAYER_SHARED",
    "KB_LAYERS",
    "KB_ROUTE",
    "Layer",
    "aget_bytes",
    "aput_bytes",
    "adelete_file",
    "cell_backend",
    "cell_root",
    "purge",
]

KB_ROUTE = "/kb/"
"""虚拟路径前缀：agent 眼里知识库的挂载根。"""

Layer = Literal["shared", "private"]
KB_LAYER_SHARED: Layer = "shared"
KB_LAYER_PRIVATE: Layer = "private"
KB_LAYERS: tuple[Layer, ...] = ("shared", "private")


def _check_layer(layer: str) -> Layer:
    if layer not in KB_LAYERS:
        raise ValueError(f"layer 必须是 shared 或 private，收到 {layer!r}")
    return layer  # type: ignore[return-value]


def store_namespace(microservice_id: str, layer: str, *, user_id: str | None = None) -> tuple[str, ...]:
    """这一层在 store 里的命名空间（见模块 docstring 的对照表）。"""
    layer = _check_layer(layer)
    if layer == KB_LAYER_SHARED:
        return ("kb", microservice_id, KB_LAYER_SHARED)
    if not user_id:
        raise ValueError("private 层必须给 user_id")
    return ("kb", microservice_id, KB_LAYER_PRIVATE, user_id)


def cell_root(name: str, layer: str, *, user_id: str | None = None) -> Path:
    """这一层在 local 模式下的磁盘目录（``name`` 已经过微服务名校验，是安全的路径段）。"""
    layer = _check_layer(layer)
    root = app_config.kb.resolved_local_root / name / layer
    if layer == KB_LAYER_PRIVATE:
        if not user_id:
            raise ValueError("private 层必须给 user_id")
        root = root / user_id
    return root


def cell_backend(
    *,
    name: str,
    microservice_id: str,
    layer: str,
    user_id: str | None = None,
    store: BaseStore | None = None,
) -> BackendProtocol:
    """取某微某层的 backend 实例（挂载与 REST 都走这里，保证两边看到同一份内容）。

    实例是轻壳（store 是两字段、filesystem 只记 root），不缓存。
    """
    kind = app_config.kb.backend_for(name)
    if kind == "store":
        ns = store_namespace(microservice_id, layer, user_id=user_id)
        return StoreBackend(namespace=lambda _rt, _ns=ns: _ns, store=store)
    if kind == "local":
        return FilesystemBackend(root_dir=cell_root(name, layer, user_id=user_id))
    # backend_for 的枚举里只有 s3 会走到这
    raise NotImplementedError(f"S3 backend 预留未实现（docs/adr/0002），微服务 {name!r} 取到 {kind!r}")


def _key(path: str) -> str:
    """层内路径 -> store key（统一带前导 ``/``，与 agent 工具读的写法同形）。

    存储边界的最后一道路径校验：挡 ``..`` / ``~``（与 ``routers/schemas/paths.py``
    同规则）—— REST 层已经用 pydantic 挡过一层，但上传文件名是裸 multipart 字段，
    走不到那个校验器。local 模式还有 ``FilesystemBackend`` 的穿越防护，这里是第三道。
    """
    raw = path.strip().replace("\\", "/")
    if not raw.strip("/"):
        raise ValueError("路径不能为空")
    if ".." in PurePosixPath(raw).parts or raw.startswith("~"):
        raise ValueError("路径不能包含 .. 或 ~")
    return "/" + raw.strip("/")


async def aput_bytes(
    *,
    name: str,
    microservice_id: str,
    layer: str,
    content: bytes,
    path: str,
    user_id: str | None = None,
    store: BaseStore | None = None,
) -> str:
    """往某层写一份原始字节（文本走 utf-8，二进制走 base64），返回层内 ``/`` 路径。

    不用 ``StoreBackend.upload_files``：它对 ``AsyncPostgresStore`` 调的是**同步** ``put``
    （异步客户端上不可用），而且 key 不带前导 ``/``，写完 ``read`` 找不到（实测）。
    local 模式则直接用 ``FilesystemBackend.aupload_files``（带路径穿越防护）。
    """
    key = _key(path)
    kind = app_config.kb.backend_for(name)
    if kind == "store":
        if store is None:
            raise RuntimeError("store backend 需要 store 实例")
        try:
            file_data = create_file_data(content.decode("utf-8"))
        except UnicodeDecodeError:
            file_data = create_file_data(
                base64.standard_b64encode(content).decode("ascii"),
                encoding="base64",
            )
        await store.aput(store_namespace(microservice_id, layer, user_id=user_id), key, file_data)
        return key
    if kind == "local":
        backend = FilesystemBackend(root_dir=cell_root(name, layer, user_id=user_id))
        response = (await backend.aupload_files([(key, content)]))[0]
        if response.error:
            raise ValueError(f"写入失败：{response.error}")
        return key
    raise NotImplementedError(f"S3 backend 预留未实现（docs/adr/0002），微服务 {name!r} 取到 {kind!r}")


async def aget_bytes(
    *,
    name: str,
    microservice_id: str,
    layer: str,
    path: str,
    user_id: str | None = None,
    store: BaseStore | None = None,
) -> bytes | None:
    """读某层一份文件的原始字节；不存在返回 ``None``（供 REST 的 read / download 共用）。"""
    key = _key(path)
    kind = app_config.kb.backend_for(name)
    if kind == "store":
        if store is None:
            raise RuntimeError("store backend 需要 store 实例")
        item = await store.aget(store_namespace(microservice_id, layer, user_id=user_id), key)
        if item is None:
            return None
        value = item.value
        raw = value.get("content") if isinstance(value, dict) else None
        if not isinstance(raw, str):
            return None
        if value.get("encoding") == "base64":
            return base64.standard_b64decode(raw)
        return raw.encode("utf-8")
    if kind == "local":
        backend = FilesystemBackend(root_dir=cell_root(name, layer, user_id=user_id))
        response = (await backend.adownload_files([key]))[0]
        if response.error or response.content is None:
            if response.error == "file_not_found":
                return None
            raise ValueError(f"读取失败：{response.error}")
        return response.content
    raise NotImplementedError(f"S3 backend 预留未实现（docs/adr/0002），微服务 {name!r} 取到 {kind!r}")


async def adelete_file(
    *,
    name: str,
    microservice_id: str,
    layer: str,
    path: str,
    user_id: str | None = None,
    store: BaseStore | None = None,
) -> bool:
    """删某层一份文件；不存在返回 ``False``（REST 的 delete 用）。"""
    key = _key(path)
    kind = app_config.kb.backend_for(name)
    if kind == "store":
        if store is None:
            raise RuntimeError("store backend 需要 store 实例")
        ns = store_namespace(microservice_id, layer, user_id=user_id)
        if await store.aget(ns, key) is None:
            return False
        await store.adelete(ns, key)
        return True
    if kind == "local":
        backend = FilesystemBackend(root_dir=cell_root(name, layer, user_id=user_id))
        response = await backend.adelete(key)
        if response.path is None:
            # FilesystemBackend 对不存在的文件回的是自然语言（"not found"），不是 FILE_NOT_FOUND 代码
            if "not found" in (response.error or "").lower():
                return False
            raise ValueError(f"删除失败：{response.error}")
        return True
    raise NotImplementedError(f"S3 backend 预留未实现（docs/adr/0002），微服务 {name!r} 取到 {kind!r}")


async def purge(store: BaseStore | None, *, microservice_id: str, name: str) -> None:
    """删微服务后清空它的内容：store 命名空间与磁盘目录**都清**，失败只记日志。

    两种都清是因为配置可能中途换过 backend（ADR 0002 换配置不迁数据）——
    按当前配置清会把另一种介质里的残留漏掉。store 清不动（agent 没起）时也继续清磁盘。
    调用方保证**先删表记录再调这里**（docs/adr/0001）。
    """
    if store is not None:
        try:
            while items := await store.asearch(("kb", microservice_id), limit=100):
                for item in items:
                    await store.adelete(item.namespace, item.key)
        except Exception:
            logger.exception("清 store 命名空间失败：microservice={} ({})", name, microservice_id)
    directory = app_config.kb.resolved_local_root / name
    if directory.exists():
        try:
            shutil.rmtree(directory)
        except OSError:
            logger.exception("清知识库目录失败：{}", directory)
