"""用户-公共空间关联表：多对多，成员一律只读。"""

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from models import BaseModel

__all__ = ["UserPublicWorkspace"]


class UserPublicWorkspace(BaseModel):
    """一个用户在一个公共空间里只有一条记录。

    **没有 permission 列**：成员一律只读，写 / 删只认 ``users.is_super``。
    这是公共空间和 :class:`~models.user_workspace.UserWorkspace` 的根本区别，
    也是当初不复用同一张表的原因（见 ``docs/adr/0001``）。

    外键带 ON DELETE CASCADE：用户或公共空间被删，关联自动消失。
    """

    __tablename__ = "user_public_workspaces"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "public_workspace_id",
            name="uq_user_public_workspaces_user_public_workspace",
        ),
    )

    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    public_workspace_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("public_workspaces.id", ondelete="CASCADE"),
        index=True,
    )

    def __repr__(self) -> str:
        return (
            f"<UserPublicWorkspace user={self.user_id} "
            f"public_workspace={self.public_workspace_id}>"
        )
