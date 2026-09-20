"""业务空间相关的请求 / 响应模型。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from models import DEFAULT_WORKSPACE, User, Workspace, WorkspacePermission
from routers.schemas.paths import NAME_PATTERN

__all__ = [
    "MemberGrant",
    "MemberOut",
    "MyWorkspaceOut",
    "WorkspaceAccessOut",
    "WorkspaceCreate",
    "WorkspaceOut",
    "WorkspaceUpdate",
]

class WorkspaceCreate(BaseModel):
    name: str = Field(
        min_length=3,
        max_length=100,
        pattern=NAME_PATTERN,
        description="空间名，全局唯一",
    )
    path: str = Field(min_length=1, max_length=512, description="工作目录路径")


class WorkspaceUpdate(BaseModel):
    """只改传了的字段。"""

    name: str | None = Field(
        default=None, min_length=3, max_length=100, pattern=NAME_PATTERN
    )
    path: str | None = Field(default=None, min_length=1, max_length=512)


class WorkspaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    path: str
    created_at: datetime
    updated_at: datetime


class MyWorkspaceOut(BaseModel):
    """带「我的权限」，用于 GET /workspaces/mine。

    ``default`` 是虚拟空间（库里没有记录，见 ``services/access.py``），所以 ``path`` /
    时间戳为空；真实空间这几个字段一定有值。
    """

    id: str
    name: str
    path: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    permission: WorkspacePermission

    @classmethod
    def of(
        cls, workspace: Workspace, permission: WorkspacePermission
    ) -> "MyWorkspaceOut":
        return cls(
            id=workspace.id,
            name=workspace.name,
            path=workspace.path,
            created_at=workspace.created_at,
            updated_at=workspace.updated_at,
            permission=permission,
        )

    @classmethod
    def default(cls) -> "MyWorkspaceOut":
        """虚拟的 default 空间：每个人都是 admin，前端拿它当日常聊天入口。"""
        return cls(
            id=DEFAULT_WORKSPACE,
            name=DEFAULT_WORKSPACE,
            permission=WorkspacePermission.ADMIN,
        )


class MemberGrant(BaseModel):
    # 用账号名（account）而不是用户 id：调用方只需要知道对方叫什么都行
    user_name: str = Field(
        min_length=3,
        max_length=50,
        pattern=NAME_PATTERN,
        description="被授权用户的账号（account），与注册时的账号规则一致",
    )
    permission: WorkspacePermission = Field(
        description="admin / editor / viewer，重复授权即改权限"
    )


class MemberOut(BaseModel):
    user_id: str
    account: str
    email: str
    permission: WorkspacePermission

    @classmethod
    def of(cls, user: User, permission: WorkspacePermission) -> "MemberOut":
        return cls(
            user_id=user.id,
            account=user.account,
            email=user.email,
            permission=permission,
        )


class WorkspaceAccessOut(BaseModel):
    """自查结果：没权限也返回 200，用 has_access 表达，不泄露空间是否存在。"""

    workspace_id: str = Field(description="回显入参的空间 id，default 表示虚拟空间")
    has_access: bool
    permission: WorkspacePermission | None = Field(
        default=None, description="我在此空间的权限：admin / editor / viewer"
    )
