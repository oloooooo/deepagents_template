"""业务空间 ORM 模型。"""

from typing import TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models import BaseModel

# 只给类型检查用，理由同 models/user.py
if TYPE_CHECKING:
    from models.user import User

__all__ = ["Workspace"]


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
