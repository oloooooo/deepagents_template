"""长期记忆路由：/memories/*。

路径带斜杠，所以「哪条记忆」进 body，不做路径参数。每个操作一条独立路径：

============================  ======  ==========  ============================================
路径                           方法     权限        说明
============================  ======  ==========  ============================================
/memories/all                 GET     viewer+     我全部空间的记忆清单（含虚拟 default 与空空间）
/memories/mine                GET     viewer+     列出我在该空间的记忆（workspace_id 可省，默认 default）
/memories/read                POST    viewer+     读一份记忆（不存在 404）
/memories/write               POST    editor+     写 / 覆盖一份记忆
/memories/upload              POST    editor+     上传文本文件（multipart，一次可多份）
/memories/delete              POST    editor+     删一份记忆（不存在 404）
============================  ======  ==========  ============================================

参数顺序：路径/查询参数 → 请求体 → 当前用户 → agent → session（鉴权先于 agent 就绪检查）。
带默认值的参数排最后是 Python 语法要求，不是忘了顺序约定。

鉴权：``user_id`` 只来自登录态（``CurrentUser.id``），workspace 用
``UserWorkspace.permission`` 校验；非成员一律 404（不泄露存在性），viewer 写记忆 403。
``workspace_id`` 不传就是虚拟的 ``default`` 空间（``models.DEFAULT_WORKSPACE``），人人 admin。

**记忆是一棵按空间分格的树**（agent 看到的是 ``/memories/{空间名}/...``）：接口按 id 寻址，
响应里回的却是 agent 眼里的完整路径，两者对齐之后用户拿到的路径能原样交给 agent
（``docs/adr/0009``）。agent 侧只有 ``default`` 那格可写，其余格子靠这里的 write / upload 投喂。
"""

from typing import Annotated

from fastapi import APIRouter, File, Form, Response, UploadFile, status

from dependencies import SessionDep
from dependencies.agent import AgentDep
from dependencies.auth import CurrentUser
from models import DEFAULT_WORKSPACE
from routers.schemas import (
    MemoryListOut,
    MemoryOut,
    MemoryPath,
    MemoryStored,
    MemoryTreeItem,
    MemoryTreeOut,
    MemoryUploadItem,
    MemoryUploadOut,
    MemoryWrite,
)
from services import MemoryService

__all__ = ["router"]

router = APIRouter(prefix="/memories", tags=["memory"])


@router.get("/all", response_model=MemoryTreeOut, summary="我全部空间的长期记忆清单")
async def list_all_memories(
    current_user: CurrentUser,
    agent: AgentDep,
    session: SessionDep,
):
    tree = await MemoryService(session, agent.memory).all(current_user)
    return MemoryTreeOut(
        workspaces=[
            MemoryTreeItem(name=name, workspace_id=workspace_id, memories=memories)
            for name, workspace_id, memories in tree
        ]
    )


@router.get("/mine", response_model=MemoryListOut, summary="我在该空间的长期记忆列表")
async def list_memories(
    current_user: CurrentUser,
    agent: AgentDep,
    session: SessionDep,
    # 带默认值的 query 只能排在必需参数后面（Python 语法），不是忘了顺序约定
    workspace_id: str = DEFAULT_WORKSPACE,
):
    memories = await MemoryService(session, agent.memory).list_memories(
        current_user, workspace_id
    )
    return MemoryListOut(workspace_id=workspace_id, memories=memories)


@router.post("/read", response_model=MemoryOut, summary="读一份长期记忆")
async def read_memory(
    payload: MemoryPath,
    current_user: CurrentUser,
    agent: AgentDep,
    session: SessionDep,
):
    path, content = await MemoryService(session, agent.memory).read(
        current_user, payload.workspace_id, payload.path
    )
    return MemoryOut(path=path, content=content)


@router.post(
    "/write",
    response_model=MemoryStored,
    summary="写一份长期记忆（需 editor+，整份覆盖）",
)
async def write_memory(
    payload: MemoryWrite,
    current_user: CurrentUser,
    agent: AgentDep,
    session: SessionDep,
):
    path = await MemoryService(session, agent.memory).write(
        current_user, payload.workspace_id, payload.path, payload.content
    )
    return MemoryStored(path=path)


@router.post(
    "/upload",
    response_model=MemoryUploadOut,
    summary="上传文本文件当记忆（需 editor+，一次可多份）",
)
async def upload_memory(
    files: Annotated[list[UploadFile], File(description="一份或多份 UTF-8 文本文件")],
    current_user: CurrentUser,
    agent: AgentDep,
    session: SessionDep,
    # 带默认值的表单字段只能排在必需参数后面（Python 语法）
    workspace_id: Annotated[str, Form(description="业务空间 id，不传就是 default")] = (
        DEFAULT_WORKSPACE
    ),
):
    # 读请求体是路由层的活（UploadFile 是 HTTP 概念），校验和落库在 service 里
    contents = [(file.filename or "", await file.read()) for file in files]
    results = await MemoryService(session, agent.memory).upload(
        current_user, workspace_id, contents
    )
    return MemoryUploadOut(
        workspace_id=workspace_id,
        results=[
            MemoryUploadItem(file=name, path=path, error=error)
            for name, path, error in results
        ],
    )


@router.post(
    "/delete",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="删一份长期记忆（需 editor+）",
)
async def delete_memory(
    payload: MemoryPath,
    current_user: CurrentUser,
    agent: AgentDep,
    session: SessionDep,
) -> None:
    await MemoryService(session, agent.memory).delete(
        current_user, payload.workspace_id, payload.path
    )
