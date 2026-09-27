"""知识库文件服务：先按可见范围把关，再调 ``agents/kb/storage`` 的字节助手。

可见范围（CONTEXT.md 的 Visibility）：

- **读**（list / read / download）：shared = 成员 ∪ super；private = 本人 ∪ super（``account`` 指定）。
  查无此人 / 不是成员一律 **同一个 404**——不泄露「这个微服务存在但你看不见」。
- **写**（write / upload / delete）：路由层就要求 super（``SuperUser``），service 不重复判；
  但微服务是否存在仍在这里查（super 也可能打错名字）。

响应路径一律是 **agent 眼里的完整写法**（``/kb/{name}/{layer}/...``），拿到就能给 agent 用。
"""

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from agents.kb.storage import (
    KB_ROUTE,
    adelete_file,
    aget_bytes,
    aput_bytes,
    cell_backend,
)
from models import Microservice, User
from repositories import (
    MicroserviceRepository,
    UserMicroserviceRepository,
    UserRepository,
)
from services.microservice import USER_NOT_FOUND

__all__ = [
    "ACCOUNT_ONLY_PRIVATE",
    "BINARY_USE_DOWNLOAD",
    "FILE_NOT_FOUND",
    "KB_NOT_FOUND",
    "KbService",
]

KB_NOT_FOUND = "知识库不存在或无权访问"
FILE_NOT_FOUND = "文件不存在"
BINARY_USE_DOWNLOAD = "二进制文件请用 /kb/files/download 取原始内容"
ACCOUNT_ONLY_PRIVATE = "account 只对 layer=private 有效"


class KbService:
    def __init__(self, session: AsyncSession, store) -> None:
        self.microservices = MicroserviceRepository(session)
        self.members = UserMicroserviceRepository(session)
        self.users = UserRepository(session)
        self.store = store

    # ---------- 把关 ----------

    async def _resolve(self, user: User, microservice: str) -> Microservice:
        ms = await self.microservices.get_by_name(microservice)
        if ms is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=KB_NOT_FOUND)
        if not user.is_super and not await self.members.is_member(
            user_id=user.id, microservice_id=ms.id
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=KB_NOT_FOUND)
        return ms

    async def _owner(self, user: User, layer: str, account: str | None) -> str | None:
        """private 层的主人 id（shared 层为 ``None``）；越权窥探他人私有 → 404。"""
        if layer == "shared":
            if account is not None:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY, detail=ACCOUNT_ONLY_PRIVATE
                )
            return None
        if account is None or account == user.account:
            return user.id
        if not user.is_super:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=KB_NOT_FOUND)
        target = await self.users.get_by_name(account)
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=USER_NOT_FOUND)
        return target.id

    def _agent_path(self, ms: Microservice, layer: str, key: str) -> str:
        return f"{KB_ROUTE}{ms.name}/{layer}{key if key.startswith('/') else '/' + key}"

    # ---------- 读（成员） ----------

    async def list(
        self, user: User, *, microservice: str, layer: str, path: str, account: str | None = None
    ) -> list[dict]:
        ms = await self._resolve(user, microservice)
        owner = await self._owner(user, layer, account)
        backend = cell_backend(
            name=ms.name, microservice_id=ms.id, layer=layer, user_id=owner, store=self.store
        )
        result = await backend.als(path)
        if result.error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=result.error)
        return [
            {
                "path": self._agent_path(ms, layer, entry["path"]),
                "is_dir": entry["is_dir"],
                "size": int(entry.get("size") or 0),
                "modified_at": entry.get("modified_at") or "",
            }
            for entry in result.entries
        ]

    async def read(
        self, user: User, *, microservice: str, layer: str, path: str, account: str | None = None
    ) -> tuple[str, str]:
        """读文本文件，返回 ``(agent 完整路径, 内容)``；二进制 422 提示走 download。"""
        ms = await self._resolve(user, microservice)
        owner = await self._owner(user, layer, account)
        data = await aget_bytes(
            name=ms.name, microservice_id=ms.id, layer=layer, path=path,
            user_id=owner, store=self.store,
        )
        if data is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=FILE_NOT_FOUND)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, detail=BINARY_USE_DOWNLOAD
            ) from exc
        return self._agent_path(ms, layer, path), text

    async def download(
        self, user: User, *, microservice: str, layer: str, path: str, account: str | None = None
    ) -> tuple[str, bytes]:
        """下载原始字节（xlsx 等二进制走这），返回 ``(agent 完整路径, 原始内容)``。"""
        ms = await self._resolve(user, microservice)
        owner = await self._owner(user, layer, account)
        data = await aget_bytes(
            name=ms.name, microservice_id=ms.id, layer=layer, path=path,
            user_id=owner, store=self.store,
        )
        if data is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=FILE_NOT_FOUND)
        return self._agent_path(ms, layer, path), data

    # ---------- 写（super，路由层已判） ----------

    async def write(
        self, user: User, *, microservice: str, layer: str, path: str,
        content: str, account: str | None = None,
    ) -> str:
        ms = await self._resolve(user, microservice)
        owner = await self._owner(user, layer, account)
        try:
            key = await self._put(ms, layer, path, content.encode("utf-8"), owner)
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        return self._agent_path(ms, layer, key)

    async def upload(
        self,
        user: User,
        *,
        microservice: str,
        layer: str,
        files: list[tuple[str, bytes]],
        account: str | None = None,
    ) -> list[tuple[str, str | None, str | None]]:
        """逐份上传（原始字节），返回 ``[(文件名, 落库路径 | None, 失败原因 | None), ...]``。

        某一份坏只坏那一份——一次上传里藏着一份坏文件就整批回滚，对用户没好处。
        """
        ms = await self._resolve(user, microservice)
        owner = await self._owner(user, layer, account)
        results: list[tuple[str, str | None, str | None]] = []
        for filename, raw in files:
            clean = filename.replace("\\", "/").strip("/")
            try:
                # 路径合法性（空 / .. / ~）由 storage._key 统一挡，这里只管拼结果
                key = await self._put(ms, layer, clean, raw, owner)
            except ValueError as exc:
                results.append((filename, None, str(exc)))
                continue
            results.append((filename, self._agent_path(ms, layer, key), None))
        return results

    async def delete(
        self, user: User, *, microservice: str, layer: str, path: str, account: str | None = None
    ) -> None:
        ms = await self._resolve(user, microservice)
        owner = await self._owner(user, layer, account)
        removed = await adelete_file(
            name=ms.name, microservice_id=ms.id, layer=layer, path=path,
            user_id=owner, store=self.store,
        )
        if not removed:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=FILE_NOT_FOUND)

    async def _put(
        self, ms: Microservice, layer: str, path: str, content: bytes, owner: str | None
    ) -> str:
        """写底层字节；``ValueError``（脏路径等）原样抛给调用方——
        ``write`` 转 422，``upload`` 变成该份的失败原因（逐份互不影响）。"""
        return await aput_bytes(
            name=ms.name, microservice_id=ms.id, layer=layer, path=path,
            content=content, user_id=owner, store=self.store,
        )
