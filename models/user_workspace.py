"""用户-业务空间关联表：多对多，权限挂在关联记录上。"""

from enum import StrEnum

from sqlalchemy import Enum, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from models import BaseModel

__all__ = ["UserWorkspace", "WorkspacePermission"]


class WorkspacePermission(StrEnum):
    """用户在某个业务空间里的权限（从大到小）。"""

    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


class UserWorkspace(BaseModel):
    """一个用户在一个空间里只有一条记录，权限写在这条记录上。

    外键带 ON DELETE CASCADE：用户或空间被删，关联自动消失。
    ``native_enum=False``：权限存成 varchar + CHECK 约束，不用 PG 枚举类型
    （加权限值时不必写 ``ALTER TYPE``）。
    ``values_callable``：入库存小写值（``viewer``），默认按成员名会存成 ``VIEWER``。
    """

    __tablename__ = "user_workspaces"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "workspace_id", name="uq_user_workspaces_user_workspace"
        ),
    )

    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    workspace_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    permission: Mapped[WorkspacePermission] = mapped_column(
        Enum(
            WorkspacePermission,
            native_enum=False,
            length=20,
            validate_strings=True,
            create_constraint=True,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        default=WorkspacePermission.VIEWER,
        # 数据库侧默认值：绕过 ORM 直接 INSERT 也是 viewer
        server_default=text("'viewer'"),
        comment="该用户在该空间的权限",
    )

    def __repr__(self) -> str:
        return (
            f"<UserWorkspace user={self.user_id} workspace={self.workspace_id} "
            f"{self.permission}>"
        )
