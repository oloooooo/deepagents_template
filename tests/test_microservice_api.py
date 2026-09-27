"""微服务管理与知识库 REST 的端到端验证（/microservices/* 与 /kb/files/*）。

前置条件：本机 PostgreSQL 可连（``user_related`` 已 ``alembic upgrade head``；``agents`` 库存在）
且 ``.env`` 里有模型配置 —— lifespan 会起 ``GeneralAgent``，但假模型保证不发网络请求。
运行方式：``uv run python tests/test_microservice_api.py``（自建自清）。

覆盖：super 建删授权（重名 409 / 幂等 204）、成员只读可见范围（非成员与不存在同为 404）、
写操作仅 super（403）、private 层按人隔离（account 越权 404）、二进制 read 422 / download 原样、
删微服务级联清内容（store 前缀行清零、名字可复用）。
"""

import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402

from agents.agent import GeneralAgent  # noqa: E402
from agents.config import AgentPostgreConfig  # noqa: E402
from config import app_config  # noqa: E402
from main import app  # noqa: E402

PASSWORD = "Passw0rd!123"
SUFFIX = uuid4().hex[:8]
ACCOUNT_FAMILY = "kb_api_"
SUPER, MEMBER, OUTSIDER = (
    f"{ACCOUNT_FAMILY}sp_{SUFFIX}",
    f"{ACCOUNT_FAMILY}mb_{SUFFIX}",
    f"{ACCOUNT_FAMILY}out_{SUFFIX}",
)
MS = f"kbms_{SUFFIX}"  # 微服务名（NAME_PATTERN：字母数字 _ . -）
CONCEPT = "订单=先扣库存"
XLSX = b"\xff\xfe\x00PK\x03\x04binary-bytes"  # 非法 utf-8，逼出 base64 存储

checks = 0
ms_id = ""


class ScriptedChatModel(FakeMessagesListChatModel):
    """只回声、绝不联网（本测试不走聊天，lifespan 起 agent 需要一个模型）。"""

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(f"收到：{messages[-1].content}"))]
        )


def step(name: str) -> None:
    global checks
    checks += 1
    print(f"  [{checks:02d}] {name}")


def db_execute(sql: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(app_config.postgresql.user.uri, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def agent_db_execute(sql: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(AgentPostgreConfig().uri, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def cleanup() -> None:
    """先拿微服务 id 清 store（prefix 是 kb.<id>.<层>），再删表记录与账号族。

    微服务按**名字族**（``kbms_`` 前缀）删而不是精确匹配：上一轮测试崩在半路留下的
    残库也一并清掉，不然下次跑清理不干净。
    """
    ids = [
        row[0]
        for row in db_execute("select id from microservices where name like %s", ("kbms\\_%",))
    ]
    for ms in ids:
        agent_db_execute("delete from store where prefix like %s", (f"kb.{ms}.%",))
    db_execute("delete from microservices where name like %s", ("kbms\\_%",))
    db_execute("delete from users where account like %s", (f"{ACCOUNT_FAMILY}%",))


def store_rows() -> int:
    """当前测试微服务在 store 里的行数（微服务已删则返回 -1）。"""
    rows = db_execute("select id from microservices where name = %s", (MS,))
    if not rows:
        return -1
    return agent_db_execute(
        "select count(*) from store where prefix like %s", (f"kb.{rows[0][0]}.%",)
    )[0][0]


def main() -> None:
    global ms_id
    cleanup()
    GeneralAgent._build_model = lambda self: ScriptedChatModel(responses=[])  # type: ignore[method-assign]
    with TestClient(app) as client:
        print("== 准备：注册 super / 成员 / 陌生人 ==")
        for account in (SUPER, MEMBER, OUTSIDER):
            resp = client.post(
                "/auth/register",
                json={"account": account, "email": f"{account}@example.com", "password": PASSWORD},
            )
            assert resp.status_code == 201, resp.text
        # is_super 没有 API 写入口，按约定直接改库
        db_execute("update users set is_super = true where account = %s", (SUPER,))

        def headers(account: str) -> dict[str, str]:
            token = client.post(
                "/auth/login", json={"account": account, "password": PASSWORD}
            ).json()["access_token"]
            return {"Authorization": f"Bearer {token}"}

        su, mem, out = headers(SUPER), headers(MEMBER), headers(OUTSIDER)

        print("== /microservices：建 ==")
        step("未登录 create -> 401")
        assert client.post("/microservices/create", json={"name": MS}).status_code == 401
        step("成员 create -> 403（只有 super 能建）")
        assert (
            client.post("/microservices/create", json={"name": MS}, headers=mem).status_code
            == 403
        )
        step("super create -> 201，回 id / name")
        resp = client.post(
            "/microservices/create",
            json={"name": MS, "description": "订单微服务"},
            headers=su,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        ms_id = body["id"]
        assert body["name"] == MS and body["description"] == "订单微服务", body
        step("重名 create -> 409")
        assert (
            client.post("/microservices/create", json={"name": MS}, headers=su).status_code
            == 409
        )
        step("非法名（带空格）-> 422")
        assert (
            client.post("/microservices/create", json={"name": "bad name"}, headers=su).status_code
            == 422
        )
        step("super list 能看到；成员 / 陌生人还看不到（没授权）")
        assert MS in [m["name"] for m in client.post("/microservices/list", headers=su).json()["microservices"]]
        assert client.post("/microservices/list", headers=mem).json()["microservices"] == []
        assert client.post("/microservices/mine", headers=su).json()["microservices"] == [], "super 没被授权过，mine 应为空"

        print("== /microservices：授权 ==")
        step("给不存在的账号授权 -> 404")
        resp = client.post(
            f"/microservices/grant/{ms_id}", json={"account": "no_such_user"}, headers=su
        )
        assert resp.status_code == 404, resp.text
        step("super grant 成员 -> 204；重复 grant 也是 204（幂等）")
        assert (
            client.post(f"/microservices/grant/{ms_id}", json={"account": MEMBER}, headers=su).status_code
            == 204
        )
        assert (
            client.post(f"/microservices/grant/{ms_id}", json={"account": MEMBER}, headers=su).status_code
            == 204
        )
        step("成员 list / mine 都看得到；陌生人两处都空（可见范围与成员关系是两个问题）")
        assert MS in [m["name"] for m in client.post("/microservices/list", headers=mem).json()["microservices"]]
        assert MS in [m["name"] for m in client.post("/microservices/mine", headers=mem).json()["microservices"]]
        assert client.post("/microservices/list", headers=out).json()["microservices"] == []
        assert client.post("/microservices/mine", headers=out).json()["microservices"] == []
        step("陌生人 grant / revoke / delete -> 403")
        for url in (f"/microservices/grant/{ms_id}", f"/microservices/revoke/{ms_id}"):
            assert client.post(url, json={"account": OUTSIDER}, headers=out).status_code == 403
        assert client.post(f"/microservices/delete/{ms_id}", headers=out).status_code == 403

        print("== /kb/files：shared 层读写 ==")
        step("super write shared/概念.md -> 200，回 agent 完整路径")
        resp = client.post(
            "/kb/files/write",
            json={"microservice": MS, "layer": "shared", "path": "概念.md", "content": CONCEPT},
            headers=su,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"path": f"/kb/{MS}/shared/概念.md"}, resp.json()
        step("成员 read -> 200 内容一致")
        resp = client.post(
            "/kb/files/read",
            json={"microservice": MS, "layer": "shared", "path": "概念.md"},
            headers=mem,
        )
        assert resp.status_code == 200 and resp.json()["content"] == CONCEPT, resp.text
        step("成员 list shared -> 列出该文件（路径是 agent 写法）")
        entries = client.post(
            "/kb/files/list",
            json={"microservice": MS, "layer": "shared"},
            headers=mem,
        ).json()["entries"]
        assert [e["path"] for e in entries] == [f"/kb/{MS}/shared/概念.md"], entries
        step("陌生人 read -> 404（与微服务不存在同一个响应，不泄露存在性）")
        resp_out = client.post(
            "/kb/files/read",
            json={"microservice": MS, "layer": "shared", "path": "概念.md"},
            headers=out,
        )
        resp_gone = client.post(
            "/kb/files/read",
            json={"microservice": "no_such_svc", "layer": "shared", "path": "概念.md"},
            headers=out,
        )
        assert resp_out.status_code == resp_gone.status_code == 404
        assert resp_out.json() == resp_gone.json(), "可见与不可见该长一个样"
        step("成员 / 陌生人 write -> 403（用户对空间一律只读）")
        for h in (mem, out):
            assert (
                client.post(
                    "/kb/files/write",
                    json={"microservice": MS, "layer": "shared", "path": "hack.md", "content": "x"},
                    headers=h,
                ).status_code
                == 403
            )
        step("路径穿越（../）-> 422；shared 层带 account -> 422")
        assert (
            client.post(
                "/kb/files/write",
                json={"microservice": MS, "layer": "shared", "path": "../evil.md", "content": "x"},
                headers=su,
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/kb/files/read",
                json={"microservice": MS, "layer": "shared", "path": "概念.md", "account": MEMBER},
                headers=su,
            ).status_code
            == 422
        )
        step("store 里确实有内容（prefix 行数 > 0）")
        assert store_rows() > 0

        print("== /kb/files：private 层按人隔离 ==")
        step("super upload 二进制到成员的 private（multipart）-> 200，回路径")
        resp = client.post(
            "/kb/files/upload",
            files=[("files", ("表.xlsx", XLSX, "application/octet-stream"))],
            data={"microservice": MS, "layer": "private", "account": MEMBER},
            headers=su,
        )
        assert resp.status_code == 200, resp.text
        item = resp.json()["results"][0]
        assert item["error"] is None and item["path"] == f"/kb/{MS}/private/表.xlsx", item
        step("成员 download -> 原始字节逐位一致")
        resp = client.post(
            "/kb/files/download",
            json={"microservice": MS, "layer": "private", "path": "表.xlsx"},
            headers=mem,
        )
        assert resp.status_code == 200 and resp.content == XLSX, (resp.status_code, resp.content)
        step("成员 read 二进制 -> 422（提示走 download）")
        resp = client.post(
            "/kb/files/read",
            json={"microservice": MS, "layer": "private", "path": "表.xlsx"},
            headers=mem,
        )
        assert resp.status_code == 422, resp.text
        step("成员窥探别人的 private（account=陌生人）-> 404；super 查看 -> 200")
        assert (
            client.post(
                "/kb/files/download",
                json={"microservice": MS, "layer": "private", "path": "表.xlsx", "account": OUTSIDER},
                headers=mem,
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/kb/files/download",
                json={"microservice": MS, "layer": "private", "path": "表.xlsx", "account": MEMBER},
                headers=su,
            ).status_code
            == 200
        )
        step("陌生人读成员的 private -> 404（连微服务都看不见）")
        assert (
            client.post(
                "/kb/files/download",
                json={"microservice": MS, "layer": "private", "path": "表.xlsx", "account": MEMBER},
                headers=out,
            ).status_code
            == 404
        )
        step("super delete 成员的 private -> 204；再删 -> 404")
        body = {"microservice": MS, "layer": "private", "path": "表.xlsx", "account": MEMBER}
        assert client.post("/kb/files/delete", json=body, headers=su).status_code == 204
        assert client.post("/kb/files/delete", json=body, headers=su).status_code == 404

        print("== /microservices：收权与删除 ==")
        step("super revoke -> 204；成员立刻读不到 shared（404）；mine 变空")
        assert (
            client.post(f"/microservices/revoke/{ms_id}", json={"account": MEMBER}, headers=su).status_code
            == 204
        )
        assert (
            client.post(
                "/kb/files/read",
                json={"microservice": MS, "layer": "shared", "path": "概念.md"},
                headers=mem,
            ).status_code
            == 404
        )
        assert client.post("/microservices/mine", headers=mem).json()["microservices"] == []
        step("重复 revoke -> 204（幂等）")
        assert (
            client.post(f"/microservices/revoke/{ms_id}", json={"account": MEMBER}, headers=su).status_code
            == 204
        )
        step("super delete -> 204，store 里该微服务的行清零")
        assert client.post(f"/microservices/delete/{ms_id}", headers=su).status_code == 204
        rows = agent_db_execute("select count(*) from store where prefix like %s", (f"kb.{ms_id}.%",))[0][0]
        assert rows == 0, f"删库后 store 还剩 {rows} 行"
        step("删完再读 -> 404；再删 -> 404；同名可复用（重建 -> 201）")
        assert (
            client.post(
                "/kb/files/read",
                json={"microservice": MS, "layer": "shared", "path": "概念.md"},
                headers=su,
            ).status_code
            == 404
        )
        assert client.post(f"/microservices/delete/{ms_id}", headers=su).status_code == 404
        resp = client.post("/microservices/create", json={"name": MS}, headers=su)
        assert resp.status_code == 201, resp.text
        step("重建后 shared 是空的（内容真的被清了，不是靠新 id 掩盖）")
        entries = client.post(
            "/kb/files/list", json={"microservice": MS, "layer": "shared"}, headers=su
        ).json()["entries"]
        assert entries == [], entries

        print("== 清理 ==")
        step("删测试微服务与账号族，store / users 无残留")
        cleanup()
        assert db_execute("select count(*) from users where account like %s", (f"{ACCOUNT_FAMILY}%",))[0][0] == 0
        assert db_execute("select count(*) from microservices where name like %s", ("kbms\\_%",))[0][0] == 0

    print(f"全部通过：{checks} 项检查")


if __name__ == "__main__":
    main()
