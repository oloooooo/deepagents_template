"""业务空间 ORM 模型。"""

from typing import TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models import BaseModel

# 只给类型检查用，理由同 models/user.py
if TYPE_CHECKING:
    from models.user import User

__all__ = ["DEFAULT_WORKSPACE", "Workspace"]

DEFAULT_WORKSPACE = "default"
"""虚拟的默认空间 id：每个登录用户都有、且都是 admin，库里**没有**对应记录。

日常聊天与记忆不指定空间时就落在这里，不必给每个用户建一条 workspaces 记录。
读写这个 id 的规则只有一处（``services/access.py``）；它也占用了空间名，
所以建/改空间时不许用（``WorkspaceService`` 409）。
"""


class Workspace(BaseModel):
    """业务空间：一个独立的工作区，name 唯一，path 指向磁盘或者记忆系统目录。"""

    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(
        String(100), index=True, unique=True, comment="业务空间名称"
    )
    path: Mapped[str] = mapped_column(String(512), comment="工作目录路径")
    # 只读视图，写入走 UserWorkspace
    users: Mapped[list["User"]] = relationship(
        secondary="user_workspaces", viewonly=True
    )

    def __repr__(self) -> str:
        return f"<Workspace {self.name!r} id={self.id}>"
