"""UserWorkspaceRepository 的端到端验证。

前置条件：本机 PostgreSQL 可用且已 ``uv run alembic upgrade head``。
运行方式：``uv run python tests/test_user_workspace_repository.py``（自建自清，不留数据）。
"""

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, func, select  # noqa: E402

from dependencies import AsyncSessionFactory  # noqa: E402
from models import User, UserWorkspace, Workspace, WorkspacePermission  # noqa: E402
from repositories import (  # noqa: E402
    UserRepository,
    UserWorkspaceRepository,
    WorkspaceRepository,
)


async def main() -> None:
    tag = uuid4().hex[:8]
    async with AsyncSessionFactory() as session:
        users, workspaces = UserRepository(session), WorkspaceRepository(session)
        links = UserWorkspaceRepository(session)

        user = await users.create(
            account=f"link-{tag}", email=f"{tag}@example.com", hashed_password="x"
        )
        ws1 = await workspaces.create(name=f"link-w1-{tag}", path="/tmp/l1")
        ws2 = await workspaces.create(name=f"link-w2-{tag}", path="/tmp/l2")
        # 后面每次 commit 都会让 ORM 对象过期，异步下再读属性会 MissingGreenlet，
        # 所以 id 先取出来放本地变量
        uid, w1, w2 = user.id, ws1.id, ws2.id

        # 首次授权
        await links.grant(user_id=uid, workspace_id=w1, permission=WorkspacePermission.EDITOR)
        assert await links.get_permission(user_id=uid, workspace_id=w1) is WorkspacePermission.EDITOR

        # 再来一次：更新权限，而不是撞唯一约束
        await links.grant(user_id=uid, workspace_id=w1, permission=WorkspacePermission.ADMIN)
        assert await links.get_permission(user_id=uid, workspace_id=w1) is WorkspacePermission.ADMIN
        assert await session.scalar(
            select(func.count()).select_from(UserWorkspace).where(UserWorkspace.user_id == uid)
        ) == 1

        # 一个用户的多个空间 + 权限
        await links.grant(user_id=uid, workspace_id=w2, permission=WorkspacePermission.VIEWER)
        assert {(w.id, p) for w, p in await links.list_by_user(uid)} == {
            (w1, WorkspacePermission.ADMIN),
            (w2, WorkspacePermission.VIEWER),
        }

        # 没关联的用户查不到东西
        assert await links.list_by_user("no-such-user") == []
        assert await links.get_permission(user_id=uid, workspace_id="no-such-ws") is None

        # 撤销
        assert await links.revoke(user_id=uid, workspace_id=w1) is True
        assert await links.get_permission(user_id=uid, workspace_id=w1) is None
        assert await links.revoke(user_id=uid, workspace_id=w1) is False

        # ON DELETE CASCADE：删空间，剩下的关联自动消失
        await session.execute(delete(Workspace).where(Workspace.id == w2))
        await session.commit()
        assert await session.scalar(
            select(func.count()).select_from(UserWorkspace).where(UserWorkspace.user_id == uid)
        ) == 0

        # 清场
        await session.execute(delete(User).where(User.id == uid))
        await session.execute(delete(Workspace).where(Workspace.id == w1))
        await session.commit()

    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
