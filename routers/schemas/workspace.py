"""业务空间相关的请求 / 响应模型。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from models import User, Workspace, WorkspacePermission

__all__ = [
    "MemberGrant",
    "MemberOut",
    "MyWorkspaceOut",
    "WorkspaceCreate",
    "WorkspaceOut",
    "WorkspaceUpdate",
]

NAME_PATTERN = r"^[A-Za-z0-9_.\-]+$"


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


class MyWorkspaceOut(WorkspaceOut):
    """带「我的权限」，用于 GET /workspaces/mine。"""

    permission: WorkspacePermission

    @classmethod
    def of(
        cls, workspace: Workspace, permission: WorkspacePermission
    ) -> "MyWorkspaceOut":
        return cls(
            **WorkspaceOut.model_validate(workspace).model_dump(), permission=permission
        )


class MemberGrant(BaseModel):
    user_id: str = Field(max_length=32, description="被授权的用户 id")
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
