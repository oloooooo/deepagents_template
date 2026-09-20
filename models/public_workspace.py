"""公共空间 ORM 模型。"""

from typing import TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models import BaseModel

# 只给类型检查用，理由同 models/user.py
if TYPE_CHECKING:
    from models.user import User

__all__ = ["PublicWorkspace"]


class PublicWorkspace(BaseModel):
    """公共空间：一组指定用户只读、super 读写删的共享空间。

    ``name`` 唯一且**不可变**（见 ``docs/adr/0002``）：它是 agent 挂载点
    ``/public/{name}/`` 的路径组成部分，改名会让所有已存在的路径失效。

    内容存在 langgraph store 里（namespace ``("public", id, "filesystem")``，见
    ``agents/agent.py`` 的 ``public_namespace``），所以这里**没有** ``path`` 字段 ——
    一个指向磁盘的路径字段只会误导人。

    成员关系在 :class:`~models.user_public_workspace.UserPublicWorkspace`，上面不带权限列：
    成员一律只读，写/删只认 ``users.is_super``（见 ``docs/adr/0001``）。
    """

    __tablename__ = "public_workspaces"

    name: Mapped[str] = mapped_column(
        String(100),
        index=True,
        unique=True,
        comment="公共空间名称，agent 挂载路径 /public/{name}/ 的一部分，不可变",
    )
    description: Mapped[str] = mapped_column(
        String(512),
        default="",
        comment="给人看的说明，出现在 REST 列表里；不注入 system prompt（见 docs/adr 与 Q17 约定）",
    )
    # 只读视图，写入走 UserPublicWorkspace
    users: Mapped[list["User"]] = relationship(
        secondary="user_public_workspaces", viewonly=True
    )

    def __repr__(self) -> str:
        return f"<PublicWorkspace {self.name!r} id={self.id}>"
