"""长期记忆路由的请求 / 响应模型。

记忆路径里带斜杠，所以不进 URL 而放 body。两个约定写死在模型里：

- **没有 user_id 字段**，且 ``extra="forbid"``：多传一个 ``user_id`` 直接 422，
  避免调用方误以为能操作别人的记忆库（归属只由登录态决定）；
- ``path`` 在这里就把 ``..`` / ``~`` / 空路径挡掉，别把脏路径带进 store。
"""

from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "MemoryListOut",
    "MemoryOut",
    "MemoryPath",
    "MemoryStored",
    "MemoryWrite",
]


class MemoryPath(BaseModel):
    """定位一份记忆：哪个空间 + 哪条路径。"""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(min_length=1, max_length=32, description="业务空间 id")
    path: str = Field(
        min_length=1,
        max_length=256,
        description="记忆路径，如 prefs.md、/memories/notes/a.md，都落到 /memories/ 下",
    )

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        if not value.strip().strip("/"):
            raise ValueError("记忆路径不能为空")
        if ".." in PurePosixPath(value.replace("\\", "/")).parts or value.startswith("~"):
            raise ValueError("记忆路径不能包含 .. 或 ~")
        return value


class MemoryWrite(MemoryPath):
    """写一份记忆：整份覆盖。"""

    content: str = Field(max_length=100_000, description="文件内容，整份覆盖")


class MemoryStored(BaseModel):
    path: str = Field(description="落库后的虚拟路径，形如 /memories/notes/a.md")


class MemoryOut(MemoryStored):
    content: str


class MemoryListOut(BaseModel):
    workspace_id: str
    memories: list[str] = Field(description="该空间下 /memories/ 里的文件路径")
