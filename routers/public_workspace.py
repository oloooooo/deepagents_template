"""公共空间路由：/public-workspaces/*。

前缀**故意不叫** ``/public``：``/public/`` 是 agent 的虚拟挂载路径（见 ``agents/public_workspace.py``），
两者混在一起会让人以为 REST 能直接操作挂载点。这里按 **id** 寻址，挂载点那边按**名字**。

权限两条线（见 ``CONTEXT.md`` 的 Visibility）：**super 全部可见**，成员只读自己被授权的。
管理动作（建 / 改 / 删 / 授权 / 撤权 / 成员列表）走路由层 ``SuperUser``；读走 ``CurrentUser``
再在 service 里过可见性；写内容用 ``SuperUser``（能到那里的必然是 super，可见性天然满足）。

每个操作一条独立路径，不靠 HTTP 方法复用同一条路径：

=========================================  ======  ==========  ==================================
路径                                         方法     权限        说明
=========================================  ======  ==========  ==================================
/public-workspaces/create                  POST    super       建公共空间
/public-workspaces/list                    GET     super       全部公共空间（管理视角）
/public-workspaces/mine                    GET     登录用户     我被授权的（super 也只看自己的）
/public-workspaces/detail/{id}             GET     可见        空间详情
/public-workspaces/update/{id}             PATCH   super       改说明（名字不可变）
/public-workspaces/delete/{id}             DELETE  super       删空间 + 清内容
/public-workspaces/grant/{id}              POST    super       body: user_name（幂等）
/public-workspaces/revoke/{id}/{user}      DELETE  super       撤权
/public-workspaces/members/{id}            GET     super       成员列表
/public-workspaces/files/list/{id}         GET     可见        列文件
/public-workspaces/files/read/{id}         POST    可见        body: path
/public-workspaces/files/write/{id}        POST    super       body: path + content
/public-workspaces/files/delete/{id}       POST    super       body: path
=========================================  ======  ==========  ==================================

参数顺序统一为「路径参数 → 请求体 → 当前用户 → agent → session」。

``delete`` 用 ``OptionalAgentDep``：删空间本身是纯数据库操作，不该因为没配模型就 503；
清 store 内容是顺带的，agent 没起来就跳过并记日志（``docs/adr/0005``）。
"""

from fastapi import APIRouter, Response, status

from dependencies import SessionDep
from dependencies.agent import AgentDep, OptionalAgentDep
from dependencies.auth import CurrentUser, SuperUser
from routers.schemas import (
    PublicFileListOut,
    PublicFileOut,
    PublicFilePath,
    PublicFileWrite,
    PublicMemberGrant,
    PublicMemberOut,
    PublicWorkspaceCreate,
    PublicWorkspaceOut,
    PublicWorkspaceUpdate,
)
from services import PublicWorkspaceService

__all__ = ["router"]

router = APIRouter(prefix="/public-workspaces", tags=["public-workspace"])


# ---------- 空间本身 ----------


@router.post(
    "/create",
    response_model=PublicWorkspaceOut,
    status_code=status.HTTP_201_CREATED,
    summary="建公共空间（需 super）",
)
async def create_public_workspace(
    payload: PublicWorkspaceCreate, current_user: SuperUser, session: SessionDep
):
    return await PublicWorkspaceService(session).create(
        name=payload.name, description=payload.description
    )


@router.get(
    "/list",
    response_model=list[PublicWorkspaceOut],
    summary="全部公共空间（需 super）",
)
async def list_public_workspaces(current_user: SuperUser, session: SessionDep):
    return await PublicWorkspaceService(session).list_all()


@router.get(
    "/mine",
    response_model=list[PublicWorkspaceOut],
    summary="我被授权的公共空间",
)
async def list_my_public_workspaces(current_user: CurrentUser, session: SessionDep):
    """super 也只看自己被授权的 —— 「我是不是成员」和「我能读哪些」是两个问题。

    要"我能读的全部"用 ``/list``（需 super）。
    """
    return await PublicWorkspaceService(session).list_mine(current_user)


@router.get(
    "/detail/{public_workspace_id}",
    response_model=PublicWorkspaceOut,
    summary="公共空间详情（需可见）",
)
async def get_public_workspace(
    public_workspace_id: str, current_user: CurrentUser, session: SessionDep
):
    return await PublicWorkspaceService(session).get(current_user, public_workspace_id)


@router.patch(
    "/update/{public_workspace_id}",
    response_model=PublicWorkspaceOut,
    summary="改公共空间的说明（需 super，名字不可变）",
)
async def update_public_workspace(
    public_workspace_id: str,
    payload: PublicWorkspaceUpdate,
    current_user: SuperUser,
    session: SessionDep,
):
    return await PublicWorkspaceService(session).update(
        public_workspace_id, description=payload.description
    )


@router.delete(
    "/delete/{public_workspace_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="删公共空间（需 super，顺带清掉内容）",
)
async def delete_public_workspace(
    public_workspace_id: str,
    current_user: SuperUser,
    session: SessionDep,
    agent: OptionalAgentDep,
) -> None:
    store = agent.public_store if agent is not None else None
    await PublicWorkspaceService(session, store).delete(public_workspace_id)


# ---------- 成员授权 ----------


@router.post(
    "/grant/{public_workspace_id}",
    response_model=dict[str, bool],
    summary="授权成员（需 super，按账号名，幂等）",
)
async def grant_public_member(
    public_workspace_id: str,
    payload: PublicMemberGrant,
    current_user: SuperUser,
    session: SessionDep,
):
    """成功返回 ``true``（而不是 204 空响应）：调用方一眼就能确认写没写进去。"""
    await PublicWorkspaceService(session).grant(
        public_workspace_id, user_name=payload.user_name
    )
    return {"result": True}


@router.delete(
    "/revoke/{public_workspace_id}/{user_name}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="撤销成员（需 super，按账号名）",
)
async def revoke_public_member(
    public_workspace_id: str,
    user_name: str,
    current_user: SuperUser,
    session: SessionDep,
) -> None:
    await PublicWorkspaceService(session).revoke(public_workspace_id, user_name)


@router.get(
    "/members/{public_workspace_id}",
    response_model=list[PublicMemberOut],
    summary="成员列表（需 super）",
)
async def list_public_members(
    public_workspace_id: str, current_user: SuperUser, session: SessionDep
):
    rows = await PublicWorkspaceService(session).list_members(public_workspace_id)
    return [PublicMemberOut.of(user) for user in rows]


# ---------- 内容 ----------


@router.get(
    "/files/list/{public_workspace_id}",
    response_model=PublicFileListOut,
    summary="列公共空间的文件（需可见）",
)
async def list_public_files(
    public_workspace_id: str,
    current_user: CurrentUser,
    agent: AgentDep,
    session: SessionDep,
):
    files = await PublicWorkspaceService(session, agent.public_store).list_files(
        current_user, public_workspace_id
    )
    return PublicFileListOut(public_workspace_id=public_workspace_id, files=files)


@router.post(
    "/files/read/{public_workspace_id}",
    response_model=PublicFileOut,
    summary="读一份公共内容（需可见）",
)
async def read_public_file(
    public_workspace_id: str,
    payload: PublicFilePath,
    current_user: CurrentUser,
    agent: AgentDep,
    session: SessionDep,
):
    content = await PublicWorkspaceService(session, agent.public_store).read_file(
        current_user, public_workspace_id, payload.path
    )
    return PublicFileOut(path=payload.path, content=content)


@router.post(
    "/files/write/{public_workspace_id}",
    response_model=PublicFileOut,
    summary="写一份公共内容（需 super，整份覆盖）",
)
async def write_public_file(
    public_workspace_id: str,
    payload: PublicFileWrite,
    current_user: SuperUser,
    agent: AgentDep,
    session: SessionDep,
):
    path = await PublicWorkspaceService(session, agent.public_store).write_file(
        public_workspace_id, payload.path, payload.content
    )
    return PublicFileOut(path=path, content=payload.content)


@router.post(
    "/files/delete/{public_workspace_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="删一份公共内容（需 super）",
)
async def delete_public_file(
    public_workspace_id: str,
    payload: PublicFilePath,
    current_user: SuperUser,
    agent: AgentDep,
    session: SessionDep,
) -> None:
    await PublicWorkspaceService(session, agent.public_store).delete_file(
        public_workspace_id, payload.path
    )
