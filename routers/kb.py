"""知识库文件路由：/kb/files/*（读 = 成员，写 = super，见 docs/adr/0003）。

============================  ======  ==========  ====================================
路径                           方法     权限        说明
============================  ======  ==========  ====================================
/kb/files/list                POST    登录        列某层目录（成员；可见范围同挂载）
/kb/files/read                POST    登录        读文本文件（二进制 422，提示走 download）
/kb/files/download            POST    登录        下载原始字节（xlsx 等，application/octet-stream）
/kb/files/write               POST    super       写文本文件（整份覆盖）
/kb/files/upload              POST    super       上传原始字节（multipart，逐份互不影响）
/kb/files/delete              POST    super       删一份文件
============================  ======  ==========  ====================================

读的可见范围：shared = 成员 ∪ super；private = 本人 ∪ super（``account`` 指定主人）。
写只在 super（``SuperUser`` 在路由层判，service 不重复判）——用户对空间一律只读。

参数顺序：请求体 → 当前用户（或 super）→ agent → 会话（鉴权先于 agent 就绪检查）。
"""

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Response, UploadFile, status

from dependencies import SessionDep
from dependencies.agent import AgentDep
from dependencies.auth import CurrentUser, SuperUser
from routers.schemas import (
    KbFileRef,
    KbList,
    KbListOut,
    KbReadOut,
    KbStored,
    KbUploadItem,
    KbUploadOut,
    KbWrite,
    Layer,
)
from services import KbService

__all__ = ["router"]

router = APIRouter(prefix="/kb/files", tags=["kb"])


@router.post("/list", response_model=KbListOut, summary="列某层目录")
async def list_kb(
    payload: KbList,
    current_user: CurrentUser,
    agent: AgentDep,
    session: SessionDep,
):
    entries = await KbService(session, agent.store).list(
        current_user,
        microservice=payload.microservice,
        layer=payload.layer,
        path=payload.path,
        account=payload.account,
    )
    return KbListOut(entries=entries)  # type: ignore[arg-type]


@router.post("/read", response_model=KbReadOut, summary="读一份文本文件")
async def read_kb(
    payload: KbFileRef,
    current_user: CurrentUser,
    agent: AgentDep,
    session: SessionDep,
):
    path, content = await KbService(session, agent.store).read(
        current_user,
        microservice=payload.microservice,
        layer=payload.layer,
        path=payload.path,
        account=payload.account,
    )
    return KbReadOut(path=path, content=content)


@router.post("/download", summary="下载原始文件（xlsx 等二进制）")
async def download_kb(
    payload: KbFileRef,
    current_user: CurrentUser,
    agent: AgentDep,
    session: SessionDep,
) -> Response:
    path, data = await KbService(session, agent.store).download(
        current_user,
        microservice=payload.microservice,
        layer=payload.layer,
        path=payload.path,
        account=payload.account,
    )
    filename = path.rsplit("/", 1)[-1]
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            # 非 ASCII 文件名（如 表.xlsx）走 RFC 5987，ASCII 回退位留着
            "Content-Disposition": (
                f"attachment; filename=\"{quote(filename)}\"; "
                f"filename*=UTF-8''{quote(filename)}"
            )
        },
    )


@router.post("/write", response_model=KbStored, summary="写一份文本文件（super）")
async def write_kb(
    payload: KbWrite,
    super_user: SuperUser,
    agent: AgentDep,
    session: SessionDep,
):
    path = await KbService(session, agent.store).write(
        super_user,
        microservice=payload.microservice,
        layer=payload.layer,
        path=payload.path,
        content=payload.content,
        account=payload.account,
    )
    return KbStored(path=path)


@router.post("/upload", response_model=KbUploadOut, summary="上传文件（super，逐份互不影响）")
async def upload_kb(
    files: Annotated[list[UploadFile], File(description="一份或多份文件（任意类型）")],
    microservice: Annotated[str, Form(max_length=100)],
    layer: Annotated[Layer, Form()],
    super_user: SuperUser,
    agent: AgentDep,
    session: SessionDep,
    account: Annotated[str | None, Form(max_length=50)] = None,
) -> KbUploadOut:
    # 读 multipart 是路由层的活（UploadFile/Form 是 HTTP 概念）；layer 是 Literal，
    # 非法值（如 "pub"）由 pydantic 在这里就 422，不会漏进 service
    contents = [(file.filename or "", await file.read()) for file in files]
    results = await KbService(session, agent.store).upload(
        super_user,
        microservice=microservice,
        layer=layer,
        files=contents,
        account=account,
    )
    return KbUploadOut(
        results=[
            KbUploadItem(file=name, path=path, error=error)
            for name, path, error in results
        ]
    )


@router.post(
    "/delete",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="删一份文件（super）",
)
async def delete_kb(
    payload: KbFileRef,
    super_user: SuperUser,
    agent: AgentDep,
    session: SessionDep,
) -> None:
    await KbService(session, agent.store).delete(
        super_user,
        microservice=payload.microservice,
        layer=payload.layer,
        path=payload.path,
        account=payload.account,
    )
