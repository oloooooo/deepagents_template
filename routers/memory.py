"""长期记忆路由：/memories/*。

路径带斜杠，所以「哪条记忆」进 body，不做路径参数。每个操作一条独立路径：

============================  ======  ==========  ============================================
路径                           方法     权限        说明
============================  ======  ==========  ============================================
/memories/all                 GET     登录        我的记忆清单
/memories/read                POST    登录        读一份记忆（不存在 404）
/memories/write               POST    登录        写 / 覆盖一份记忆
/memories/upload              POST    登录        上传文本文件（multipart，一次可多份）
/memories/delete              POST    登录        删一份记忆（不存在 404）
============================  ======  ==========  ============================================

参数顺序：路径/查询参数 → 请求体 → 当前用户 → agent（鉴权先于 agent 就绪检查）。

鉴权：``user_id`` 只来自登录态（``CurrentUser.id``）——记忆按用户隔离，不存在跨用户访问。

**接口按 id 寻址，响应回的却是 agent 眼里的完整路径**（``/memories/notes/a.md``），两者对齐
之后用户拿到的路径能原样交给 agent（``docs/adr/0009``）。
"""

from typing import Annotated

from fastapi import APIRouter, File, Response, UploadFile, status

from dependencies.agent import AgentDep
from dependencies.auth import CurrentUser
from routers.schemas import (
    MemoryListOut,
    MemoryOut,
    MemoryPath,
    MemoryStored,
    MemoryUploadItem,
    MemoryUploadOut,
    MemoryWrite,
)
from services import MemoryService

__all__ = ["router"]

router = APIRouter(prefix="/memories", tags=["memory"])


@router.get("/all", response_model=MemoryListOut, summary="我的长期记忆清单")
async def list_all_memories(
    current_user: CurrentUser,
    agent: AgentDep,
):
    memories = await MemoryService(agent.memory).list_memories(current_user)
    return MemoryListOut(memories=memories)


@router.post("/read", response_model=MemoryOut, summary="读一份长期记忆")
async def read_memory(
    payload: MemoryPath,
    current_user: CurrentUser,
    agent: AgentDep,
):
    path, content = await MemoryService(agent.memory).read(current_user, payload.path)
    return MemoryOut(path=path, content=content)


@router.post(
    "/write",
    response_model=MemoryStored,
    summary="写一份长期记忆（整份覆盖）",
)
async def write_memory(
    payload: MemoryWrite,
    current_user: CurrentUser,
    agent: AgentDep,
):
    path = await MemoryService(agent.memory).write(
        current_user, payload.path, payload.content
    )
    return MemoryStored(path=path)


@router.post(
    "/upload",
    response_model=MemoryUploadOut,
    summary="上传文本文件当记忆（一次可多份）",
)
async def upload_memory(
    files: Annotated[list[UploadFile], File(description="一份或多份 UTF-8 文本文件")],
    current_user: CurrentUser,
    agent: AgentDep,
):
    # 读请求体是路由层的活（UploadFile 是 HTTP 概念），校验和落库在 service 里
    contents = [(file.filename or "", await file.read()) for file in files]
    results = await MemoryService(agent.memory).upload(current_user, contents)
    return MemoryUploadOut(
        results=[
            MemoryUploadItem(file=name, path=path, error=error)
            for name, path, error in results
        ],
    )


@router.post(
    "/delete",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="删一份长期记忆",
)
async def delete_memory(
    payload: MemoryPath,
    current_user: CurrentUser,
    agent: AgentDep,
) -> None:
    await MemoryService(agent.memory).delete(current_user, payload.path)
