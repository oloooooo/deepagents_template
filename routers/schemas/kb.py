"""知识库文件操作的请求 / 响应模型（/kb/files/*）。

三个约定：

- ``path`` 是**层内相对路径**（如 ``概念.md``、``docs/a.md``），带不带前导 ``/`` 都收，
  响应回的是 agent 眼里的完整路径（``/kb/{微服务名}/{layer}/...``），用户拿到能原样给 agent；
- ``layer`` 只有 ``shared`` / ``private`` 两层（见 ``agents/kb/storage.py``）；
- ``account`` 只对 ``layer=private`` 有意义：指定**谁的**私有空间（super 用），缺省是自己。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from routers.schemas.paths import NAME_PATTERN, safe_store_path

__all__ = [
    "KbEntry",
    "KbFileRef",
    "KbList",
    "KbListOut",
    "KbReadOut",
    "KbStored",
    "KbUploadItem",
    "KbUploadOut",
    "KbWrite",
    "Layer",
]

Layer = Literal["shared", "private"]


def _safe_path(value: str) -> str:
    return safe_store_path(value, label="知识库路径")


class _KbRef(BaseModel):
    """微服务 + 层 + 层内路径（三段定位一份库内文件）。"""

    model_config = ConfigDict(extra="forbid")

    microservice: str = Field(
        min_length=1, max_length=100, pattern=NAME_PATTERN, description="微服务名"
    )
    layer: Layer = Field(description="shared = 共享知识；private = 某人的私有文档")
    path: str = Field(min_length=1, max_length=256, description="层内相对路径")
    account: str | None = Field(
        None, max_length=50, description="layer=private 时指定主人（super 用），缺省本人"
    )


class KbFileRef(_KbRef):
    """读 / 删 / 下载：定位一份文件（``path`` 不能为空、不能穿越）。"""

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        return _safe_path(value)


class KbList(_KbRef):
    """列目录：``path`` 是目录，缺省层根。"""

    path: str = Field(default="/", max_length=256, description="层内目录路径，缺省层根")

    @field_validator("path")
    @classmethod
    def _check_dir(cls, value: str) -> str:
        # 层根（"/" / "" / "."）合法；其余按文件路径规则挡 .. / ~
        return "/" if not value.strip().strip("/") else _safe_path(value)


class KbWrite(KbFileRef):
    """写一份文本文件（整份覆盖）。"""

    content: str = Field(max_length=100_000, description="文件内容，整份覆盖")


class KbEntry(BaseModel):
    """一条目录项：``path`` 是 agent 眼里的完整路径。"""

    path: str
    is_dir: bool
    size: int = 0
    modified_at: str = ""


class KbListOut(BaseModel):
    entries: list[KbEntry]


class KbReadOut(BaseModel):
    path: str = Field(description="agent 完整路径，形如 /kb/{name}/shared/概念.md")
    content: str


class KbStored(BaseModel):
    path: str


class KbUploadItem(BaseModel):
    """一次上传里某一份文件的结果（逐份互不影响）。"""

    file: str
    path: str | None = None
    error: str | None = None


class KbUploadOut(BaseModel):
    results: list[KbUploadItem]
