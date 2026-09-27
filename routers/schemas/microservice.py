"""微服务（知识库空间）管理的请求 / 响应模型（docs/adr/0001）。

``name`` 就是 agent 挂载路径 ``/kb/{name}/`` 的一段，字符集与 ``NAME_PATTERN`` 一致，
只允许字母数字和 ``_ . -``，保证能安全地当路径组件。
"""

from pydantic import BaseModel, ConfigDict, Field

from routers.schemas.paths import NAME_PATTERN

__all__ = [
    "MicroserviceCreate",
    "MicroserviceGrant",
    "MicroserviceListOut",
    "MicroserviceOut",
]


class MicroserviceCreate(BaseModel):
    """创建微服务 = 创建它的知识库空间（一一对应）。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=100,
        pattern=NAME_PATTERN,
        description="微服务名，也是 agent 路径 /kb/{name}/ 的格子名",
    )
    description: str | None = Field(None, max_length=512, description="给人看的说明")


class MicroserviceGrant(BaseModel):
    """授权 / 收权的目标：按账号精确匹配用户。"""

    model_config = ConfigDict(extra="forbid")

    account: str = Field(
        min_length=1, max_length=50, description="目标用户账号（不是邮箱）"
    )


class MicroserviceOut(BaseModel):
    id: str
    name: str
    description: str | None = None


class MicroserviceListOut(BaseModel):
    microservices: list[MicroserviceOut]
