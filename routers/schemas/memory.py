"""长期记忆路由的请求 / 响应模型。

记忆路径里带斜杠，所以不进 URL 而放 body。两个约定写死在模型里：

- **没有 user_id 字段**，且 ``extra="forbid"``：多传一个 ``user_id`` 直接 422，
  避免调用方误以为能操作别人的记忆库（归属只由登录态决定）；
- ``path`` 在这里就把 ``..`` / ``~`` / 空路径挡掉，别把脏路径带进 store。
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from models import DEFAULT_WORKSPACE
from routers.schemas.paths import safe_store_path

__all__ = [
    "MemoryListOut",
    "MemoryOut",
    "MemoryPath",
    "MemoryStored",
    "MemoryTreeItem",
    "MemoryTreeOut",
    "MemoryUploadItem",
    "MemoryUploadOut",
    "MemoryWrite",
]


class MemoryPath(BaseModel):
    """定位一份记忆：哪个空间 + 哪条路径。"""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(
        DEFAULT_WORKSPACE,
        min_length=1,
        max_length=32,
        description="业务空间 id；不传就落虚拟的 default 空间（日常聊天）",
    )
    path: str = Field(
        min_length=1,
        max_length=256,
        description=(
            "格子内路径（如 prefs.md、notes/a.md）；也收 /memories/notes/a.md、"
            "/memories/{空间名}/notes/a.md 两种 agent 写法"
        ),
    )

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        return safe_store_path(value, label="记忆路径")


class MemoryWrite(MemoryPath):
    """写一份记忆：整份覆盖。"""

    content: str = Field(max_length=100_000, description="文件内容，整份覆盖")


class MemoryStored(BaseModel):
    path: str = Field(
        description="落库后的虚拟路径，形如 /memories/{空间名}/notes/a.md（agent 能直接读到）"
    )


class MemoryOut(MemoryStored):
    content: str


class MemoryListOut(BaseModel):
    workspace_id: str
    memories: list[str] = Field(
        description="该空间下的文件虚拟路径（形如 /memories/{空间名}/notes/a.md）"
    )


class MemoryTreeItem(BaseModel):
    """记忆树里的一格：一个业务空间。"""

    name: str = Field(description="空间名（agent 挂载路径 /memories/{名字}/ 里那一段）")
    workspace_id: str
    memories: list[str] = Field(description="该空间下的文件虚拟路径，空空间是空列表")


class MemoryTreeOut(BaseModel):
    workspaces: list[MemoryTreeItem] = Field(
        description="我参与的全部空间（含虚拟 default 与没有任何文件的空间）"
    )


class MemoryUploadItem(BaseModel):
    """一次上传里某一份文件的结果。"""

    file: str = Field(description="客户端给的文件名")
    path: str | None = Field(None, description="落库后的虚拟路径（失败时为空）")
    error: str | None = Field(None, description="这一份失败的原因（成功时为空）")


class MemoryUploadOut(BaseModel):
    """逐份结果：某一份失败不影响其它份，所以不整批回滚。"""

    workspace_id: str
    results: list[MemoryUploadItem]
