"""公共空间 REST API 的端到端验证（super 规则版）。

前置条件：业务库已 ``uv run alembic upgrade head``，agent 库可用（公共内容存在 langgraph
store 里，连接参数见 ``agents/config.py``）。
运行方式：``uv run python tests/test_public_workspace_api.py``（自建自清）。

**不需要模型 API**：内容读写只用到 agent 的 store，不跑对话。
``users.is_super`` 没有 API 写入口，这里故意用裸 SQL 改，顺便证明「只能在数据库里赋权」。
"""

import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agents.config import AgentPostgreConfig  # noqa: E402
from config import app_config  # noqa: E402
from main import app  # noqa: E402

PASSWORD = "Passw0rd!123"
SUFFIX = uuid4().hex[:8]
ROOT = f"pwroot_{SUFFIX}"
USER = f"pwuser_{SUFFIX}"
NOBODY = f"pwnobody_{SUFFIX}"
PUB = f"pw-{SUFFIX}"
PUB2 = f"pw2-{SUFFIX}"

checks = 0


def step(name: str) -> None:
    global checks
    checks += 1
    print(f"  [{checks:02d}] {name}")


def db_execute(sql: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(app_config.postgresql.user.uri, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def store_rows(public_workspace_id: str) -> int:
    """该公共空间在 langgraph store 里还有几条内容。

    store 表的 ``prefix`` 是命名空间用 ``.`` 拼起来的，所以 ``("public", id, "filesystem")``
    对应 ``public.{id}.filesystem``。
    """
    with psycopg.connect(AgentPostgreConfig().uri, autocommit=True) as conn:
        cur = conn.execute(
            "select count(*) from store where prefix = %s",
            (f"public.{public_workspace_id}.filesystem",),
        )
        return cur.fetchone()[0]


def cleanup() -> None:
    # psycopg3 里 % 必须走参数，不能直接写在 SQL 里
    db_execute("delete from public_workspaces where name like %s", ("pw%",))
    db_execute(
        "delete from users where account like %s or account like %s or account like %s",
        ("pwroot_%", "pwuser_%", "pwnobody_%"),
    )


def main() -> None:
    cleanup()
    with TestClient(app) as client:
        print("== 准备两个普通用户 ==")
        for account in (ROOT, USER, NOBODY):
            resp = client.post(
                "/auth/register",
                json={"account": account, "email": f"{account}@example.com", "password": PASSWORD},
            )
            assert resp.status_code == 201, resp.text

        def login(account: str) -> dict[str, str]:
            token = client.post(
                "/auth/login", json={"account": account, "password": PASSWORD}
            ).json()["access_token"]
            return {"Authorization": f"Bearer {token}"}

        root_h, user_h, nobody_h = login(ROOT), login(USER), login(NOBODY)

        print("== 普通用户不能建公共空间 ==")
        step("非 super POST /public-workspaces/create -> 403")
        resp = client.post("/public-workspaces/create", json={"name": PUB}, headers=user_h)
        assert resp.status_code == 403, resp.text
        assert db_execute("select count(*) from public_workspaces where name = %s", (PUB,)) == [(0,)]

        step("未登录 POST /public-workspaces/create -> 401")
        assert client.post("/public-workspaces/create", json={"name": PUB}).status_code == 401

        print("== 数据库里授予 super ==")
        step("裸 SQL 置 is_super=true 后重登，/auth/me 能读到")
        db_execute("update users set is_super = true where account = %s", (ROOT,))
        root_h = login(ROOT)
        assert client.get("/auth/me", headers=root_h).json()["is_super"] is True

        print("== super 建公共空间 ==")
        step("非法 name（带空格）-> 422；description 超长 -> 422")
        assert client.post("/public-workspaces/create", json={"name": "bad name"}, headers=root_h).status_code == 422
        assert client.post(
            "/public-workspaces/create", json={"name": PUB, "description": "x" * 513}, headers=root_h
        ).status_code == 422

        step(f"POST /public-workspaces/create -> 201，拿到 id；description 默认空串")
        resp = client.post("/public-workspaces/create", json={"name": PUB}, headers=root_h)
        assert resp.status_code == 201, resp.text
        created = resp.json()
        pub_id = created["id"]
        assert created["name"] == PUB and created["description"] == "", created
        resp = client.post(
            "/public-workspaces/create", json={"name": PUB2, "description": "给新人看的"}, headers=root_h
        )
        assert resp.status_code == 201, resp.text
        pub2_id = resp.json()["id"]

        step("重名 -> 409（库里有记录，但不占用 default 这种虚拟名）")
        assert client.post("/public-workspaces/create", json={"name": PUB}, headers=root_h).status_code == 409

        print("== super 的可见范围 vs 成员关系（Q16：两个不同的问题）==")
        step("super 的 /list 是全部，/mine 只有自己被授权的（这里是空）")
        assert {w["name"] for w in client.get("/public-workspaces/list", headers=root_h).json()} >= {PUB, PUB2}
        assert client.get("/public-workspaces/mine", headers=root_h).json() == []
        assert client.get("/public-workspaces/mine", headers=user_h).json() == []

        step("非 super 访问 /list -> 403")
        assert client.get("/public-workspaces/list", headers=user_h).status_code == 403

        print("== super 写内容，成员才能读 ==")
        step("super POST /files/write -> 200，路径被规范化")
        resp = client.post(
            f"/public-workspaces/files/write/{pub_id}",
            json={"path": "/notes/a.md", "content": "公共规范 v1"},
            headers=root_h,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"path": "notes/a.md", "content": "公共规范 v1"}, resp.json()
        assert store_rows(pub_id) == 1

        step("路径校验：'..' / '~' / 空 -> 422")
        for bad in ("../escape.md", "~/x.md", "   "):
            assert client.post(
                f"/public-workspaces/files/write/{pub_id}",
                json={"path": bad, "content": "x"},
                headers=root_h,
            ).status_code == 422, bad

        step("未授权的人读 / 列 / 看详情 -> 404（不是 403，不泄露存在性）")
        assert client.get(f"/public-workspaces/detail/{pub_id}", headers=user_h).status_code == 404
        assert client.get(f"/public-workspaces/files/list/{pub_id}", headers=user_h).status_code == 404
        assert client.post(
            f"/public-workspaces/files/read/{pub_id}", json={"path": "notes/a.md"}, headers=user_h
        ).status_code == 404
        assert client.get("/public-workspaces/detail/does-not-exist", headers=user_h).status_code == 404

        step("super 不是成员也读得到（可见范围 = 成员 ∪ super）")
        assert client.get(f"/public-workspaces/detail/{pub_id}", headers=root_h).status_code == 200
        assert client.get(f"/public-workspaces/files/list/{pub_id}", headers=root_h).json() == {
            "public_workspace_id": pub_id,
            "files": ["notes/a.md"],
        }

        print("== 授权 / 撤权 ==")
        step("POST /grant 按账号名 -> true；重复授权幂等，仍然 200")
        resp = client.post(
            f"/public-workspaces/grant/{pub_id}", json={"user_name": USER}, headers=root_h
        )
        assert resp.status_code == 200 and resp.json() == {"result": True}, resp.text
        assert client.post(
            f"/public-workspaces/grant/{pub_id}", json={"user_name": USER}, headers=root_h
        ).status_code == 200
        assert db_execute(
            "select count(*) from user_public_workspaces where public_workspace_id = %s", (pub_id,)
        ) == [(1,)]

        step("授权后：/mine 有它、能读、能列；members 列表里没有 permission 字段")
        mine = client.get("/public-workspaces/mine", headers=user_h).json()
        assert [w["name"] for w in mine] == [PUB], mine
        assert client.post(
            f"/public-workspaces/files/read/{pub_id}", json={"path": "notes/a.md"}, headers=user_h
        ).json() == {"path": "notes/a.md", "content": "公共规范 v1"}
        members = client.get(f"/public-workspaces/members/{pub_id}", headers=root_h).json()
        assert members == [{"user_id": members[0]["user_id"], "account": USER, "email": f"{USER}@example.com"}], members

        step("成员写 / 删内容 -> 403（只有 super 能改公共内容）")
        assert client.post(
            f"/public-workspaces/files/write/{pub_id}",
            json={"path": "notes/a.md", "content": "偷偷改"},
            headers=user_h,
        ).status_code == 403
        assert client.post(
            f"/public-workspaces/files/delete/{pub_id}", json={"path": "notes/a.md"}, headers=user_h
        ).status_code == 403

        step("成员不能授权别人 / 不能删空间 -> 403")
        assert client.post(
            f"/public-workspaces/grant/{pub_id}", json={"user_name": NOBODY}, headers=user_h
        ).status_code == 403
        assert client.delete(f"/public-workspaces/delete/{pub_id}", headers=user_h).status_code == 403
        assert client.get(f"/public-workspaces/members/{pub_id}", headers=user_h).status_code == 403

        step("授给不存在的账号 -> 404；给不存在的空间授权 -> 404")
        assert client.post(
            f"/public-workspaces/grant/{pub_id}", json={"user_name": "no_such_user"}, headers=root_h
        ).status_code == 404
        assert client.post(
            "/public-workspaces/grant/does-not-exist", json={"user_name": USER}, headers=root_h
        ).status_code == 404

        step("读不存在的文件 -> 404")
        assert client.post(
            f"/public-workspaces/files/read/{pub_id}", json={"path": "nope.md"}, headers=root_h
        ).status_code == 404

        print("== 改说明：名字不可变 ==")
        step("PATCH /update 改 description -> 200；body 里塞 name 被忽略（extra=forbid -> 422）")
        resp = client.patch(
            f"/public-workspaces/update/{pub_id}", json={"description": "新的说明"}, headers=root_h
        )
        assert resp.status_code == 200 and resp.json()["description"] == "新的说明", resp.text
        assert resp.json()["name"] == PUB, resp.json()
        assert client.patch(
            f"/public-workspaces/update/{pub_id}", json={"name": "renamed"}, headers=root_h
        ).status_code == 422
        assert client.patch(
            f"/public-workspaces/update/{pub_id}", json={"description": "x"}, headers=user_h
        ).status_code == 403

        print("== 撤权 ==")
        step("DELETE /revoke -> 204，被撤的人读不到、/mine 也空了")
        assert client.delete(f"/public-workspaces/revoke/{pub_id}/{USER}", headers=root_h).status_code == 204
        assert client.post(
            f"/public-workspaces/files/read/{pub_id}", json={"path": "notes/a.md"}, headers=user_h
        ).status_code == 404
        assert client.get("/public-workspaces/mine", headers=user_h).json() == []

        step("再撤一次 -> 404；不存在的账号 -> 404")
        assert client.delete(f"/public-workspaces/revoke/{pub_id}/{USER}", headers=root_h).status_code == 404
        assert client.delete(f"/public-workspaces/revoke/{pub_id}/no_such_user", headers=root_h).status_code == 404

        print("== 删空间：表和数据一起清 ==")
        step("先确认 PUB2 里也有内容，删完 store 里不能留")
        assert client.post(
            f"/public-workspaces/files/write/{pub2_id}",
            json={"path": "onboarding.md", "content": "欢迎"},
            headers=root_h,
        ).status_code == 200
        assert store_rows(pub2_id) == 1

        step("DELETE /public-workspaces/delete/{id} -> 204，表里和 store 里都没了")
        assert client.delete(f"/public-workspaces/delete/{pub2_id}", headers=root_h).status_code == 204
        assert db_execute("select count(*) from public_workspaces where id = %s", (pub2_id,)) == [(0,)]
        assert store_rows(pub2_id) == 0, "删空间时内容没清干净"

        step("再删一次 -> 404；未登录 -> 401")
        assert client.delete(f"/public-workspaces/delete/{pub2_id}", headers=root_h).status_code == 404
        assert client.delete(f"/public-workspaces/delete/{pub_id}").status_code == 401

        step("关联记录随空间级联删除（删 PUB 前先授权一次）")
        assert client.post(
            f"/public-workspaces/grant/{pub_id}", json={"user_name": USER}, headers=root_h
        ).status_code == 200
        assert client.delete(f"/public-workspaces/delete/{pub_id}", headers=root_h).status_code == 204
        assert db_execute(
            "select count(*) from user_public_workspaces where public_workspace_id = %s", (pub_id,)
        ) == [(0,)]
        assert store_rows(pub_id) == 0

    cleanup()
    print(f"\n全部通过（{checks} 项）。")


if __name__ == "__main__":
    main()
