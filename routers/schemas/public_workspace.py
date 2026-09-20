"""公共空间路由的请求 / 响应模型。

两个约定写死在模型里：

- **没有 user_id 字段**，且 ``extra="forbid"``：谁能读只由登录态和授权记录决定；
- ``path`` 在这里就把 ``..`` / ``~`` / 空路径挡掉（和 ``MemoryPath`` 同一套校验）。

``PublicWorkspaceUpdate`` **没有 name 字段**：公共空间名是 agent 挂载路径
``/public/{name}/`` 的一段，改了所有已存路径就失效（``docs/adr/0002``），只能删了重建。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from models import User
from routers.schemas.paths import NAME_PATTERN, safe_store_path

__all__ = [
    "PublicFileListOut",
    "PublicFileOut",
    "PublicFilePath",
    "PublicFileWrite",
    "PublicMemberGrant",
    "PublicMemberOut",
    "PublicWorkspaceCreate",
    "PublicWorkspaceOut",
    "PublicWorkspaceUpdate",
]


class PublicWorkspaceCreate(BaseModel):
    name: str = Field(
        min_length=3,
        max_length=100,
        pattern=NAME_PATTERN,
        description="公共空间名，全局唯一且不可变；它是 agent 挂载路径 /public/{name}/ 的一段",
    )
    description: str = Field(
        default="",
        max_length=512,
        description="给人看的说明，出现在列表里；不注入 system prompt",
    )


class PublicWorkspaceUpdate(BaseModel):
    """只改说明。名字不可变，见模块 docstring。"""

    description: str = Field(max_length=512, description="新的说明，整份覆盖")


class PublicWorkspaceOut(BaseModel):
    """直接由 ORM 对象序列化（``from_attributes``），service 返回的就是 ``PublicWorkspace``。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str
    created_at: datetime
    updated_at: datetime


class PublicMemberGrant(BaseModel):
    # 用账号名（account）而不是用户 id，和 /workspaces/grant 保持一致
    user_name: str = Field(
        min_length=3,
        max_length=50,
        pattern=NAME_PATTERN,
        description="被授权用户的账号（account）；重复授权是幂等的",
    )


class PublicMemberOut(BaseModel):
    """公共空间的成员。**没有 permission 字段** —— 成员一律只读，见 docs/adr/0001。"""

    user_id: str
    account: str
    email: str

    @classmethod
    def of(cls, user: User) -> "PublicMemberOut":
        return cls(user_id=user.id, account=user.account, email=user.email)


class PublicFilePath(BaseModel):
    """定位一份公共内容：路径在 body 里（可能带斜杠，不适合进 URL）。"""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        min_length=1,
        max_length=256,
        description="公共空间内的相对路径，如 notes/a.md 或 /notes/a.md",
    )

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        return safe_store_path(value, label="公共空间路径")


class PublicFileWrite(PublicFilePath):
    """写一份公共内容：整份覆盖。"""

    content: str = Field(max_length=100_000, description="文件内容，整份覆盖")


class PublicFileOut(PublicFilePath):
    content: str


class PublicFileListOut(BaseModel):
    public_workspace_id: str
    files: list[str] = Field(description="该公共空间下的文件路径")
