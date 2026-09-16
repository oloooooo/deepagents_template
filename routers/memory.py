"""长期记忆路由：/memories/*。

路径带斜杠，所以「哪条记忆」进 body，不做路径参数。每个操作一条独立路径：

============================  ======  ==========  ===================================
路径                           方法     权限        说明
============================  ======  ==========  ===================================
/memories/mine                GET     viewer+     列出我在该空间 /memories/ 下的文件
/memories/read                POST    viewer+     读一份记忆（不存在 404）
/memories/write               POST    editor+     写/覆盖一份记忆
/memories/delete              POST    editor+     删一份记忆（不存在 404）
============================  ======  ==========  ===================================

参数顺序：路径/查询参数 → 请求体 → 当前用户 → agent → session（鉴权先于 agent 就绪检查）。
鉴权：``user_id`` 只来自登录态（``CurrentUser.id``），workspace 用
``UserWorkspace.permission`` 校验；非成员一律 404（不泄露存在性），viewer 写记忆 403。
"""

from fastapi import APIRouter, Response, status

from dependencies import SessionDep
from dependencies.agent import AgentDep
from dependencies.auth import CurrentUser
from routers.schemas import MemoryListOut, MemoryOut, MemoryPath, MemoryStored, MemoryWrite
from services import MemoryService

__all__ = ["router"]

router = APIRouter(prefix="/memories", tags=["memory"])


@router.get("/mine", response_model=MemoryListOut, summary="我在该空间的长期记忆列表")
async def list_memories(
    workspace_id: str,
    current_user: CurrentUser,
    agent: AgentDep,
    session: SessionDep,
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
    content = await MemoryService(session, agent.memory).read(
        current_user, payload.workspace_id, payload.path
    )
    return MemoryOut(path=payload.path, content=content)


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
