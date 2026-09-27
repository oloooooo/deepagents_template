"""微服务管理路由：/microservices/*（建删授权 = super，见 docs/adr/0001）。

============================  ======  ==========  ====================================
路径                           方法     权限        说明
============================  ======  ==========  ====================================
/microservices/create         POST    super       建微服务 = 建知识库空间（重名 409）
/microservices/delete/{id}    POST    super       删微服务 = 删知识库（级联清内容）
/microservices/list           POST    登录        我能读哪些（super 全部 / 普通=成员）
/microservices/mine           POST    登录        我是不是成员（纯成员关系，与 list 两回事）
/microservices/grant/{id}     POST    super       授权（按账号，幂等）
/microservices/revoke/{id}    POST    super       收权（按账号，幂等）
============================  ======  ==========  ====================================

动词进路径、一个操作一条独立路径；``list`` / ``mine`` 用 POST 是因为「查询条件可能进 body」，
与项目里 ``/memories/read`` 等既有 POST 查询保持一致。

``delete`` 要连 store 一起清内容，所以带上 ``AgentDep``（agent 没起就是 503——
内容清不掉时不让删，避免留下清不掉的孤儿数据）。
"""

from typing import Annotated

from fastapi import APIRouter, Path, Response, status

from dependencies import SessionDep
from dependencies.agent import AgentDep
from dependencies.auth import CurrentUser, SuperUser
from routers.schemas import (
    MicroserviceCreate,
    MicroserviceGrant,
    MicroserviceListOut,
    MicroserviceOut,
)
from services import MicroserviceService

__all__ = ["router"]

router = APIRouter(prefix="/microservices", tags=["microservice"])


def _out(ms) -> MicroserviceOut:
    return MicroserviceOut(id=ms.id, name=ms.name, description=ms.description)


@router.post(
    "/create",
    status_code=status.HTTP_201_CREATED,
    response_model=MicroserviceOut,
    summary="创建微服务（= 创建知识库空间）",
)
async def create_microservice(
    payload: MicroserviceCreate,
    super_user: SuperUser,
    session: SessionDep,
):
    ms = await MicroserviceService(session).create(
        name=payload.name, description=payload.description
    )
    return _out(ms)


@router.post(
    "/delete/{microservice_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="删除微服务（= 删除知识库，级联清内容）",
)
async def delete_microservice(
    microservice_id: Annotated[str, Path(min_length=1, max_length=32)],
    super_user: SuperUser,
    agent: AgentDep,
    session: SessionDep,
) -> None:
    await MicroserviceService(session, store=agent.store).delete(microservice_id)


@router.post("/list", response_model=MicroserviceListOut, summary="我能读哪些微服务")
async def list_microservices(
    current_user: CurrentUser,
    session: SessionDep,
):
    return MicroserviceListOut(
        microservices=[_out(ms) for ms in await MicroserviceService(session).list(current_user)]
    )


@router.post("/mine", response_model=MicroserviceListOut, summary="我加入的微服务（纯成员关系）")
async def my_microservices(
    current_user: CurrentUser,
    session: SessionDep,
):
    return MicroserviceListOut(
        microservices=[_out(ms) for ms in await MicroserviceService(session).mine(current_user)]
    )


@router.post(
    "/grant/{microservice_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="授权用户加入微服务",
)
async def grant_microservice(
    microservice_id: Annotated[str, Path(min_length=1, max_length=32)],
    payload: MicroserviceGrant,
    super_user: SuperUser,
    session: SessionDep,
) -> None:
    await MicroserviceService(session).grant(microservice_id, payload.account)


@router.post(
    "/revoke/{microservice_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="收回用户对微服务的访问",
)
async def revoke_microservice(
    microservice_id: Annotated[str, Path(min_length=1, max_length=32)],
    payload: MicroserviceGrant,
    super_user: SuperUser,
    session: SessionDep,
) -> None:
    await MicroserviceService(session).revoke(microservice_id, payload.account)
