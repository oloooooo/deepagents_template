"""业务空间路由：空间 CRUD + 成员授权。

约定：路由只收参、调 service、返回；请求/响应模型在 routers/schemas 里。
参数顺序统一为「路径参数 → 请求体 → 当前用户 → session」。
写接口用 ``SuperUser``（路由层就把非 super 挡掉），读接口用 ``CurrentUser``。

每个操作一个独立路径，不靠 HTTP 方法复用同一条路径：

===========================  ========  ================
路径                          方法      权限
===========================  ========  ================
/workspaces/mine             GET       成员
/workspaces/create           POST      super
/workspaces/detail/{id}      GET       成员
/workspaces/update/{id}      PATCH     super
/workspaces/delete/{id}      DELETE    super
/workspaces/members/{id}     GET       成员
/workspaces/grant/{id}       POST      super
/workspaces/revoke/{id}/{u}  DELETE    super
===========================  ========  ================
"""

from fastapi import APIRouter, Response, status

from dependencies import SessionDep
from dependencies.auth import CurrentUser, SuperUser
from routers.schemas import (
    MemberGrant,
    MemberOut,
    MyWorkspaceOut,
    WorkspaceCreate,
    WorkspaceOut,
    WorkspaceUpdate,
)
from services import WorkspaceService

__all__ = ["router"]

router = APIRouter(prefix="/workspaces", tags=["workspace"])


@router.get("/mine", response_model=list[MyWorkspaceOut], summary="我参与的空间")
async def list_my_workspaces(current_user: CurrentUser, session: SessionDep):
    rows = await WorkspaceService(session).list_mine(current_user)
    return [MyWorkspaceOut.of(workspace, permission) for workspace, permission in rows]


@router.post(
    "/create",
    response_model=WorkspaceOut,
    status_code=status.HTTP_201_CREATED,
    summary="建空间（需 super），创建者自动成为该空间 admin",
)
async def create_workspace(
    payload: WorkspaceCreate, current_user: SuperUser, session: SessionDep
):
    return await WorkspaceService(session).create(
        current_user, name=payload.name, path=payload.path
    )


@router.get(
    "/detail/{workspace_id}", response_model=WorkspaceOut, summary="空间详情（需成员）"
)
async def get_workspace(
    workspace_id: str, current_user: CurrentUser, session: SessionDep
):
    return await WorkspaceService(session).get(current_user, workspace_id)


@router.patch(
    "/update/{workspace_id}",
    response_model=WorkspaceOut,
    summary="改空间（需 super）",
)
async def update_workspace(
    workspace_id: str,
    payload: WorkspaceUpdate,
    current_user: SuperUser,
    session: SessionDep,
):
    return await WorkspaceService(session).update(
        workspace_id, name=payload.name, path=payload.path
    )


@router.delete(
    "/delete/{workspace_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="删空间（需 super）",
)
async def delete_workspace(
    workspace_id: str, current_user: SuperUser, session: SessionDep
) -> None:
    await WorkspaceService(session).delete(workspace_id)


@router.get(
    "/members/{workspace_id}",
    response_model=list[MemberOut],
    summary="成员列表（需成员）",
)
async def list_members(
    workspace_id: str, current_user: CurrentUser, session: SessionDep
):
    rows = await WorkspaceService(session).list_members(current_user, workspace_id)
    return [MemberOut.of(user, permission) for user, permission in rows]


@router.post(
    "/grant/{workspace_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="加成员 / 改权限（需 super）",
)
async def grant_member(
    workspace_id: str,
    payload: MemberGrant,
    current_user: SuperUser,
    session: SessionDep,
) -> None:
    await WorkspaceService(session).grant_member(
        workspace_id, user_id=payload.user_id, permission=payload.permission
    )


@router.delete(
    "/revoke/{workspace_id}/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="移除成员（需 super）",
)
async def revoke_member(
    workspace_id: str,
    user_id: str,
    current_user: SuperUser,
    session: SessionDep,
) -> None:
    await WorkspaceService(session).revoke_member(workspace_id, user_id)
