"""路由的请求 / 响应模型（pydantic），按资源分文件。"""

from .auth import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UserOut,
)
from .workspace import (
    MemberGrant,
    MemberOut,
    MyWorkspaceOut,
    WorkspaceAccessOut,
    WorkspaceCreate,
    WorkspaceOut,
    WorkspaceUpdate,
)

__all__ = [
    "LoginRequest",
    "MemberGrant",
    "MemberOut",
    "MyWorkspaceOut",
    "RefreshRequest",
    "RegisterRequest",
    "TokenPair",
    "UserOut",
    "WorkspaceAccessOut",
    "WorkspaceCreate",
    "WorkspaceOut",
    "WorkspaceUpdate",
]
