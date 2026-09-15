"""业务空间 + 成员授权的端到端验证（super 规则版）。

前置条件：本机 PostgreSQL 可用且已 ``uv run alembic upgrade head``。
运行方式：``uv run python tests/test_workspace_api.py``（自建自清）。

``users.is_super`` 没有任何 API/repository 写入口，这里故意用裸 SQL 改，
顺便证明「只能在数据库里赋权」。
"""

import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from config import app_config  # noqa: E402
from main import app  # noqa: E402

PASSWORD = "Passw0rd!123"
SUFFIX = uuid4().hex[:8]
ROOT = f"wsroot_{SUFFIX}"
USER = f"wsuser_{SUFFIX}"
WS_NAME = f"ws-{SUFFIX}"

checks = 0


def step(name: str) -> None:
    global checks
    checks += 1
    print(f"  [{checks:02d}] {name}")


def db_execute(sql: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(app_config.postgresql.user.uri, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def cleanup() -> None:
    # psycopg3 里 % 必须走参数，不能直接写在 SQL 里
    db_execute("delete from workspaces where name like %s", ("ws-%",))
    db_execute(
        "delete from users where account like %s or account like %s",
        ("wsroot_%", "wsuser_%"),
    )


def main() -> None:
    cleanup()
    with TestClient(app) as client:
        print("== 准备两个普通用户 ==")
        for account, email in ((ROOT, f"{ROOT}@example.com"), (USER, f"{USER}@example.com")):
            resp = client.post(
                "/auth/register",
                json={"account": account, "email": email, "password": PASSWORD},
            )
            assert resp.status_code == 201, resp.text

        root_token = client.post("/auth/login", json={"account": ROOT, "password": PASSWORD}).json()["access_token"]
        user_token = client.post("/auth/login", json={"account": USER, "password": PASSWORD}).json()["access_token"]
        root_h = {"Authorization": f"Bearer {root_token}"}
        user_h = {"Authorization": f"Bearer {user_token}"}

        step("新注册用户 is_super=false，注册请求里塞 is_super 也无效")
        assert client.get("/auth/me", headers=root_h).json()["is_super"] is False
        dup = client.post(
            "/auth/register",
            json={
                "account": f"wssneak_{SUFFIX}",
                "email": f"wssneak_{SUFFIX}@example.com",
                "password": PASSWORD,
                "is_super": True,
            },
        )
        assert dup.status_code == 201, dup.text
        assert dup.json()["is_super"] is False, dup.json()
        db_execute("delete from users where account = %s", (f"wssneak_{SUFFIX}",))

        print("== 普通用户不能建空间 ==")
        step("非 super POST /workspaces/create -> 403")
        resp = client.post("/workspaces/create", json={"name": WS_NAME, "path": "/tmp/ws"}, headers=user_h)
        assert resp.status_code == 403, resp.text
        assert db_execute("select count(*) from workspaces where name = %s", (WS_NAME,)) == [(0,)]

        step("未登录 POST /workspaces/create -> 401")
        assert client.post("/workspaces/create", json={"name": WS_NAME, "path": "/tmp/ws"}).status_code == 401

        print("== 数据库里授予 super ==")
        step("裸 SQL 置 is_super=true 后重登，/auth/me 能读到")
        db_execute("update users set is_super = true where account = %s", (ROOT,))
        root_token = client.post("/auth/login", json={"account": ROOT, "password": PASSWORD}).json()["access_token"]
        root_h = {"Authorization": f"Bearer {root_token}"}
        assert client.get("/auth/me", headers=root_h).json()["is_super"] is True

        print("== super 建空间 ==")
        step("非法 name（带空格）/ path 为空 -> 422")
        assert client.post("/workspaces/create", json={"name": "bad name", "path": "/tmp"}, headers=root_h).status_code == 422
        assert client.post("/workspaces/create", json={"name": WS_NAME, "path": ""}, headers=root_h).status_code == 422

        step("POST /workspaces/create -> 201，创建者自动成为该空间 admin")
        resp = client.post("/workspaces/create", json={"name": WS_NAME, "path": "/tmp/ws"}, headers=root_h)
        assert resp.status_code == 201, resp.text
        workspace_id = resp.json()["id"]
        assert db_execute(
            "select permission from user_workspaces where workspace_id = %s", (workspace_id,)
        ) == [("admin",)]

        step("重名 -> 409")
        assert client.post("/workspaces/create", json={"name": WS_NAME, "path": "/tmp/other"}, headers=root_h).status_code == 409

        step("GET /workspaces/mine 带上我的权限")
        mine = client.get("/workspaces/mine", headers=root_h).json()[0]
        assert (mine["id"], mine["permission"]) == (workspace_id, "admin"), mine

        print("== 非成员看不到 ==")
        step("普通用户的列表为空 / 详情 404 / 成员列表 404")
        assert client.get("/workspaces/mine", headers=user_h).json() == []
        assert client.get(f"/workspaces/detail/{workspace_id}", headers=user_h).status_code == 404
        assert client.get(f"/workspaces/members/{workspace_id}", headers=user_h).status_code == 404

        step("不存在的空间 id -> 404")
        assert client.get("/workspaces/detail/ffffffffffffffffffffffffffffffff", headers=root_h).status_code == 404

        print("== 成员管理只有 super 能动 ==")
        step("普通用户给自己加权限 -> 403（哪怕空间存在也不再是 404）")
        user_id = client.get("/auth/me", headers=user_h).json()["id"]
        resp = client.post(
            f"/workspaces/grant/{workspace_id}",
            json={"user_id": user_id, "permission": "admin"},
            headers=user_h,
        )
        assert resp.status_code == 403, resp.text

        step("super 加成员 viewer -> 204")
        resp = client.post(
            f"/workspaces/grant/{workspace_id}",
            json={"user_id": user_id, "permission": "viewer"},
            headers=root_h,
        )
        assert resp.status_code == 204 and resp.content == b"", (resp.status_code, resp.content)

        step("成员能看详情和成员列表了，列表里是 viewer")
        assert client.get(f"/workspaces/detail/{workspace_id}", headers=user_h).status_code == 200
        assert client.get("/workspaces/mine", headers=user_h).json()[0]["permission"] == "viewer"
        assert len(client.get(f"/workspaces/members/{workspace_id}", headers=user_h).json()) == 2

        print("== 空间自己的 admin 权限也不能写 ==")
        step("把普通用户提到 admin")
        assert client.post(
            f"/workspaces/grant/{workspace_id}",
            json={"user_id": user_id, "permission": "admin"},
            headers=root_h,
        ).status_code == 204
        rows = db_execute(
            "select permission, count(*) from user_workspaces where workspace_id = %s and user_id = %s group by permission",
            (workspace_id, user_id),
        )
        assert rows == [("admin", 1)], rows

        step("admin（非 super）改空间 / 删空间 / 加成员 / 移除成员 -> 全部 403")
        assert client.patch(f"/workspaces/update/{workspace_id}", json={"path": "/tmp/hack"}, headers=user_h).status_code == 403
        assert client.delete(f"/workspaces/delete/{workspace_id}", headers=user_h).status_code == 403
        assert client.post(
            f"/workspaces/grant/{workspace_id}",
            json={"user_id": user_id, "permission": "viewer"},
            headers=user_h,
        ).status_code == 403
        assert client.delete(f"/workspaces/revoke/{workspace_id}/{user_id}", headers=user_h).status_code == 403

        step("库里的数据没被上面几下改动")
        assert db_execute("select count(*) from workspaces where id = %s", (workspace_id,)) == [(1,)]
        assert db_execute(
            "select permission from user_workspaces where workspace_id = %s and user_id = %s",
            (workspace_id, user_id),
        ) == [("admin",)]

        print("== super 改空间 ==")
        step("授权的边界：不存在的用户 404、非法权限值 422")
        assert client.post(
            f"/workspaces/grant/{workspace_id}",
            json={"user_id": "0" * 32, "permission": "viewer"},
            headers=root_h,
        ).status_code == 404
        assert client.post(
            f"/workspaces/grant/{workspace_id}",
            json={"user_id": user_id, "permission": "owner"},
            headers=root_h,
        ).status_code == 422

        step("改成已有名字 -> 409；改新名字 -> 200")
        assert client.patch(f"/workspaces/update/{workspace_id}", json={"name": WS_NAME}, headers=root_h).status_code == 200
        new_name = f"ws-renamed-{SUFFIX}"
        assert client.patch(f"/workspaces/update/{workspace_id}", json={"name": new_name}, headers=root_h).json()["name"] == new_name

        step("super 移除成员 -> 204，被移除的人又看不到 -> 404")
        assert client.delete(f"/workspaces/revoke/{workspace_id}/{user_id}", headers=root_h).status_code == 204
        assert client.get(f"/workspaces/detail/{workspace_id}", headers=user_h).status_code == 404

        step("再移除一次 -> 404")
        assert client.delete(f"/workspaces/revoke/{workspace_id}/{user_id}", headers=root_h).status_code == 404

        print("== super 删空间 ==")
        step("DELETE /workspaces/delete/{id} -> 204，库里空间和关联都没了")
        assert client.delete(f"/workspaces/delete/{workspace_id}", headers=root_h).status_code == 204
        assert client.get(f"/workspaces/detail/{workspace_id}", headers=root_h).status_code == 404
        assert db_execute("select count(*) from workspaces where id = %s", (workspace_id,)) == [(0,)]
        assert db_execute("select count(*) from user_workspaces where workspace_id = %s", (workspace_id,)) == [(0,)]
        assert client.delete(f"/workspaces/delete/{workspace_id}", headers=root_h).status_code == 404

    cleanup()
    print(f"\n全部通过：{checks} 项检查")


if __name__ == "__main__":
    try:
        main()
    finally:
        cleanup()
