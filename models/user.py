"""用户表 ORM 模型。"""

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String, false, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models import BaseModel

# 只给类型检查用：运行时导入会和 models.workspace 循环，
# 注解里的字符串由 SQLAlchemy 自己从 registry 解析。
if TYPE_CHECKING:
    from models.workspace import Workspace

__all__ = ["User"]


class User(BaseModel):
    """账号 / 邮箱 / 密码哈希 / refresh token。

    表名用 ``users``：``user`` 是 PostgreSQL 保留字，建表与查询都要加引号。
    """

    __tablename__ = "users"

    account: Mapped[str] = mapped_column(
        String(50), index=True, unique=True, comment="登录账号"
    )
    email: Mapped[str] = mapped_column(
        String(255), index=True, unique=True, comment="邮箱，也可用于登录"
    )
    hashed_password: Mapped[str] = mapped_column(String(100), comment="bcrypt 哈希")
    refresh_token: Mapped[str | None] = mapped_column(
        String(512),
        default=None,
        comment="当前有效的 refresh token，登录/刷新时轮换，登出后置空",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), comment="是否启用"
    )
    # 超级用户标志：故意不提供任何 repository/API 写入口，只能直接改库
    # （注册、登录、找回密码都碰不到这个字段）。
    is_super: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), comment="超级用户，只能直接改库"
    )
    # 只读视图：带权限的关联走 UserWorkspace，写入请直接操作它
    workspaces: Mapped[list["Workspace"]] = relationship(
        secondary="user_workspaces", viewonly=True
    )

    def __repr__(self) -> str:
        return f"<User {self.account!r} id={self.id}>"
