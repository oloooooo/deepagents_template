"""用户-空间多对多 + 权限字段的最小验证（内存 SQLite，无需 PostgreSQL）。

运行方式：``uv run python tests/test_user_workspace.py``。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import datetime  # noqa: E402

from sqlalchemy import create_engine, select, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from models import Base, User, UserWorkspace, Workspace, WorkspacePermission  # noqa: E402


def main() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        u = User(account="alice", email="a@x.com", hashed_password="h")
        w1, w2 = Workspace(name="proj-a", path="/tmp/a"), Workspace(name="proj-b", path="/tmp/b")
        session.add_all([u, w1, w2])
        session.flush()

        session.add_all(
            [
                UserWorkspace(user_id=u.id, workspace_id=w1.id, permission="admin"),
                UserWorkspace(user_id=u.id, workspace_id=w2.id),
            ]
        )
        session.commit()

        # 显式权限能读回；没传 permission 的默认 viewer（且入库存小写）
        perms = dict(session.execute(select(UserWorkspace.workspace_id, UserWorkspace.permission)).all())
        assert perms[w1.id] == WorkspacePermission.ADMIN, perms
        assert perms[w2.id] == WorkspacePermission.VIEWER, perms
        raw = session.execute(text("select permission from user_workspaces where workspace_id = :w"), {"w": w2.id}).scalar()
        assert raw == "viewer", raw

        # 数据库 CHECK 拦住非法权限值
        try:
            session.execute(
                text(
                    "insert into user_workspaces"
                    " (id, user_id, workspace_id, permission, created_at, updated_at)"
                    " values ('dup', :u, :w, 'owner', :now, :now)"
                ),
                {"u": u.id, "w": w1.id, "now": datetime.now()},
            )
        except IntegrityError:
            session.rollback()
        else:
            raise AssertionError("非法权限值应当被 CHECK 约束拒绝")

        # 多对多两侧的只读关系都通
        assert {w.name for w in u.workspaces} == {"proj-a", "proj-b"}
        assert [x.account for x in w1.users] == ["alice"]

        # 同一用户在同一空间不能重复
        session.add(UserWorkspace(user_id=u.id, workspace_id=w1.id))
        try:
            session.commit()
        except Exception:
            session.rollback()
        else:
            raise AssertionError("重复关联应当被唯一约束拒绝")

    print("ok")


if __name__ == "__main__":
    main()
