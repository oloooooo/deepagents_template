"""ORM 基类：统一约束命名 + 公共字段（id / created_at / updated_at）。"""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import DateTime, MetaData, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = ["Base", "BaseModel", "User"]

# 统一索引/约束命名，便于 alembic 生成可预期的迁移脚本
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class BaseModel(Base):
    """所有业务表继承它，自带主键与时间戳。"""

    __abstract__ = True

    id: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=lambda: uuid4().hex
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


# 放在末尾导入，保证 Base/BaseModel 先定义好（避免循环导入）
from .user import User  # noqa: E402
