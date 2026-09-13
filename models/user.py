"""用户表 ORM 模型。"""

from sqlalchemy import Boolean, String, true
from sqlalchemy.orm import Mapped, mapped_column

from models import BaseModel

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

    def __repr__(self) -> str:
        return f"<User {self.account!r} id={self.id}>"
