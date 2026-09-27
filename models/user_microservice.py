"""用户-微服务成员关系：二元（是 / 不是成员），没有权限列（见 docs/adr/0001）。"""

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from models import BaseModel

__all__ = ["UserMicroservice"]


class UserMicroservice(BaseModel):
    """一个用户在一个微服务里只有一条记录。

    **没有 permission 列**：v1 除 super 外人人只读，成员关系本身就是权限，
    等级没有可判定的行为差异（旧体系三级权限已废，见 CONTEXT.md 的 Member）。

    外键带 ON DELETE CASCADE：用户或微服务被删，关联自动消失。
    """

    __tablename__ = "user_microservices"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "microservice_id",
            name="uq_user_microservices_user_microservice",
        ),
    )

    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    microservice_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("microservices.id", ondelete="CASCADE"), index=True
    )

    def __repr__(self) -> str:
        return (
            f"<UserMicroservice user={self.user_id} "
            f"microservice={self.microservice_id}>"
        )
