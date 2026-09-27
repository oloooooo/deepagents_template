"""微服务 ORM 模型：微服务即知识库空间（见 docs/adr/0001）。"""

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from models import BaseModel

__all__ = ["Microservice"]


class Microservice(BaseModel):
    """一个业务服务，同时**就是**它的知识库空间（一一对应，没有独立空间表）。

    删微服务 = 删知识库：成员关系靠外键 CASCADE 消失，存储内容由服务层清理
    （先删表记录、再清内容，失败只记日志）。

    将来 MCP / SKILL 注册表挂 ``microservices.id``（见 docs/adr/0001），现在不建。
    ``name`` 是唯一键，也是 agent 挂载路径 ``/kb/{name}/`` 的格子名，建 / 改时要当作
    路径组件校验（不允许 ``/``、空格等），由 service 层负责。
    """

    __tablename__ = "microservices"

    name: Mapped[str] = mapped_column(
        String(100), index=True, unique=True, comment="微服务名，也是 /kb/ 下的格子名"
    )
    description: Mapped[str | None] = mapped_column(
        String(512), default=None, comment="给人看的说明，出现在 REST 列表里"
    )

    def __repr__(self) -> str:
        return f"<Microservice {self.name!r} id={self.id}>"
