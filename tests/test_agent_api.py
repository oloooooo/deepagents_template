"""Agent 相关 HTTP 路由的端到端验证（/memories/*，后续补 /chat/*）。

前置条件：本机 PostgreSQL 可连（``user_related`` 已 ``alembic upgrade head``；``agents`` 库存在）
且 ``.env`` 里有模型配置 —— lifespan 会起 ``GeneralAgent``，但 /memories 路由本身不调模型。
运行方式：``uv run python tests/test_agent_api.py``（自建自清）。

覆盖：记忆库的写/读/列/删契约、viewer 只读、非成员 404、请求体塞 user_id 被拒、跨空间隔离。
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
from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402

from agents.agent import GeneralAgent  # noqa: E402
from agents.config import AgentPostgreConfig  # noqa: E402
from config import app_config  # noqa: E402
from main import app  # noqa: E402

PASSWORD = "Passw0rd!123"
SUFFIX = uuid4().hex[:8]
# 账号族前缀：cleanup 按它清理历史残留，不要改成太通用的前缀
ACCOUNT_FAMILY = "agt_api_"
ROOT, EDITOR, VIEWER, OUTSIDER = (
    f"{ACCOUNT_FAMILY}root_{SUFFIX}",
    f"{ACCOUNT_FAMILY}ed_{SUFFIX}",
    f"{ACCOUNT_FAMILY}vw_{SUFFIX}",
    f"{ACCOUNT_FAMILY}out_{SUFFIX}",
)
WS_NAME, WS2_NAME = f"ws-api-{SUFFIX}", f"ws-api2-{SUFFIX}"

checks = 0


class ScriptedChatModel(FakeMessagesListChatModel):
    """测试用假模型：按上下文造回答，绝不发网络请求。

    「记住」→ 调 ``write_file`` 写 ``/memories/prefs.md``（命中 interrupt 规则）；
    工具结果回来→回一句「已记住」；其它输入回声，便于断言。
    """

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001, ANN003, ANN201
        last = messages[-1]
        if isinstance(last, ToolMessage):
            message = AIMessage("已记住")
        elif "记住" in str(last.content):
            message = AIMessage(
                "",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {
                            "file_path": "/memories/prefs.md",
                            "content": "喜欢简短回答",
                        },
                        "id": "call_1",
                    }
                ],
            )
        elif "临时" in str(last.content):
            # 普通路径走 StateBackend（会话内临时文件），不进长期记忆
            message = AIMessage(
                "",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"file_path": "/tmp/note.txt", "content": "临时笔记"},
                        "id": "call_2",
                    }
                ],
            )
        else:
            message = AIMessage(f"收到：{last.content}")
        return ChatResult(generations=[ChatGeneration(message=message)])


SCRIPTED_MODEL = ScriptedChatModel(responses=[])


def step(name: str) -> None:
    global checks
    checks += 1
    print(f"  [{checks:02d}] {name}")


def db_execute(sql: str, params: tuple = ()) -> list[tuple]:
    """业务库（users / workspaces）。"""
    with psycopg.connect(app_config.postgresql.user.uri, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def agent_db_execute(sql: str, params: tuple = ()) -> list[tuple]:
    """agent 库（checkpoints / store 长期记忆）。"""
    with psycopg.connect(AgentPostgreConfig().uri, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def cleanup() -> None:
    """先按账号族拿 user_id，再清 agent 库（thread_id 前缀是 user_id，不是账号名）。"""
    like = f"{ACCOUNT_FAMILY}%"
    user_ids = [row[0] for row in db_execute("select id from users where account like %s", (like,))]
    for user_id in user_ids:
        agent_db_execute("delete from store where prefix like %s", (f"{user_id}.%",))
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            agent_db_execute(f"delete from {table} where thread_id like %s", (f"{user_id}:%",))
    db_execute("delete from workspaces where name like %s", ("ws-api%",))
    db_execute("delete from users where account like %s", (like,))


def main() -> None:
    cleanup()
    # /chat/* 一律走假模型：不联网、结果确定（真实 LLM 联调不在本次范围）
    GeneralAgent._build_model = lambda self: SCRIPTED_MODEL  # type: ignore[method-assign]
    with TestClient(app) as client:
        print("== 准备：super + editor / viewer / 陌生人 ==")
        for account in (ROOT, EDITOR, VIEWER, OUTSIDER):
            resp = client.post(
                "/auth/register",
                json={"account": account, "email": f"{account}@example.com", "password": PASSWORD},
            )
            assert resp.status_code == 201, resp.text
        db_execute("update users set is_super = true where account = %s", (ROOT,))

        def headers(account: str) -> dict[str, str]:
            token = client.post(
                "/auth/login", json={"account": account, "password": PASSWORD}
            ).json()["access_token"]
            return {"Authorization": f"Bearer {token}"}

        root_h, editor_h, viewer_h, outsider_h = (
            headers(ROOT),
            headers(EDITOR),
            headers(VIEWER),
            headers(OUTSIDER),
        )
        editor_id = db_execute("select id from users where account = %s", (EDITOR,))[0][0]

        print("== super 建两个空间，授权 editor / viewer ==")
        ws = client.post(
            "/workspaces/create", json={"name": WS_NAME, "path": "/tmp/ws-api"}, headers=root_h
        )
        assert ws.status_code == 201, ws.text
        ws_id = ws.json()["id"]
        ws2 = client.post(
            "/workspaces/create", json={"name": WS2_NAME, "path": "/tmp/ws-api2"}, headers=root_h
        )
        assert ws2.status_code == 201, ws2.text
        ws2_id = ws2.json()["id"]
        for target, permission in ((EDITOR, "editor"), (VIEWER, "viewer")):
            for workspace_id in (ws_id, ws2_id):
                resp = client.post(
                    f"/workspaces/grant/{workspace_id}",
                    json={"user_name": target, "permission": permission},
                    headers=root_h,
                )
                assert resp.status_code == 200, resp.text

        print("== /memories/* 契约 ==")
        step("未登录 GET /memories/mine -> 401")
        assert client.get("/memories/mine", params={"workspace_id": ws_id}).status_code == 401

        step("editor POST /memories/write -> 200 且返回落库路径")
        resp = client.post(
            "/memories/write",
            json={"workspace_id": ws_id, "path": "prefs.md", "content": "喜欢简短回答"},
            headers=editor_h,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"path": "/memories/prefs.md"}, resp.json()

        step("editor POST /memories/read -> 200 且内容一致")
        resp = client.post(
            "/memories/read",
            json={"workspace_id": ws_id, "path": "/memories/prefs.md"},
            headers=editor_h,
        )
        assert resp.status_code == 200 and resp.json() == {
            "path": "/memories/prefs.md",
            "content": "喜欢简短回答",
        }, resp.text

        step("editor GET /memories/mine -> 200 且列出该文件")
        resp = client.get("/memories/mine", params={"workspace_id": ws_id}, headers=editor_h)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"workspace_id": ws_id, "memories": ["/memories/prefs.md"]}, resp.json()

        step("覆写同一路径 -> 200 且内容更新")
        resp = client.post(
            "/memories/write",
            json={"workspace_id": ws_id, "path": "prefs.md", "content": "改成详细回答"},
            headers=editor_h,
        )
        assert resp.status_code == 200, resp.text
        assert (
            client.post(
                "/memories/read",
                json={"workspace_id": ws_id, "path": "prefs.md"},
                headers=editor_h,
            ).json()["content"]
            == "改成详细回答"
        )

        print("== /memories/* 鉴权 ==")
        step("viewer 读 -> 200（同空间不同人是各自的记忆库，这里是 viewer 自己的）")
        assert (
            client.get("/memories/mine", params={"workspace_id": ws_id}, headers=viewer_h).json()[
                "memories"
            ]
            == []
        )
        step("viewer 写 -> 403")
        resp = client.post(
            "/memories/write",
            json={"workspace_id": ws_id, "path": "vw.md", "content": "x"},
            headers=viewer_h,
        )
        assert resp.status_code == 403, resp.text
        step("viewer 删 -> 403")
        resp = client.post(
            "/memories/delete",
            json={"workspace_id": ws_id, "path": "prefs.md"},
            headers=viewer_h,
        )
        assert resp.status_code == 403, resp.text

        step("非成员 list / read / write -> 404（不泄露空间是否存在）")
        assert (
            client.get("/memories/mine", params={"workspace_id": ws_id}, headers=outsider_h).status_code
            == 404
        )
        assert (
            client.post(
                "/memories/read",
                json={"workspace_id": ws_id, "path": "prefs.md"},
                headers=outsider_h,
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/memories/write",
                json={"workspace_id": ws_id, "path": "x.md", "content": "x"},
                headers=outsider_h,
            ).status_code
            == 404
        )
        step("不存在的空间 id -> 404")
        assert (
            client.get("/memories/mine", params={"workspace_id": "0" * 32}, headers=editor_h).status_code
            == 404
        )

        step("请求体里塞 user_id -> 422（归属只认登录态）")
        resp = client.post(
            "/memories/write",
            json={"workspace_id": ws_id, "path": "sneak.md", "content": "x", "user_id": ROOT},
            headers=editor_h,
        )
        assert resp.status_code == 422, resp.text
        step("路径穿越 / 空路径 -> 422")
        for bad in ("../../etc/passwd", "~/.ssh/id_rsa", "/"):
            resp = client.post(
                "/memories/write",
                json={"workspace_id": ws_id, "path": bad, "content": "x"},
                headers=editor_h,
            )
            assert resp.status_code == 422, (bad, resp.text)

        print("== 隔离：同一用户跨空间 / 同空间跨用户 ==")
        step("editor 在 ws2 的列表为空（记忆按 (user, workspace) 隔离）")
        assert (
            client.get("/memories/mine", params={"workspace_id": ws2_id}, headers=editor_h).json()[
                "memories"
            ]
            == []
        )
        step("editor 在 ws2 读 ws1 的路径 -> 404")
        assert (
            client.post(
                "/memories/read",
                json={"workspace_id": ws2_id, "path": "prefs.md"},
                headers=editor_h,
            ).status_code
            == 404
        )
        step("viewer 在 ws1 读不到 editor 写的文件 -> 404")
        assert (
            client.post(
                "/memories/read",
                json={"workspace_id": ws_id, "path": "prefs.md"},
                headers=viewer_h,
            ).status_code
            == 404
        )

        print("== 删除 ==")
        step("editor POST /memories/delete -> 204")
        resp = client.post(
            "/memories/delete",
            json={"workspace_id": ws_id, "path": "prefs.md"},
            headers=editor_h,
        )
        assert resp.status_code == 204, resp.text
        step("再读 -> 404，再删 -> 404")
        assert (
            client.post(
                "/memories/read",
                json={"workspace_id": ws_id, "path": "prefs.md"},
                headers=editor_h,
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/memories/delete",
                json={"workspace_id": ws_id, "path": "prefs.md"},
                headers=editor_h,
            ).status_code
            == 404
        )
        step("库里确实没留下记忆")
        assert (
            agent_db_execute(
                "select count(*) from store where prefix like %s", (f"{editor_id}.%",)
            )
            == [(0,)]
        )

        print("== /chat/* 契约 ==")
        step("未登录 -> 401；非成员 -> 404")
        assert (
            client.post(
                "/chat/send", json={"workspace_id": ws_id, "message": "hi"}
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/chat/send",
                json={"workspace_id": ws_id, "message": "hi"},
                headers=outsider_h,
            ).status_code
            == 404
        )

        step("editor POST /chat/send -> 200，返回 thread_id 与回答")
        resp = client.post(
            "/chat/send",
            json={"workspace_id": ws_id, "message": "你好"},
            headers=editor_h,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        thread_id = body["thread_id"]
        assert body["answer"] == "收到：你好" and body["interrupt"] is None, body

        step("GET /chat/state/{thread} -> 200，带 workspace_id 与消息数")
        state = client.get(f"/chat/state/{thread_id}", headers=editor_h)
        assert state.status_code == 200, state.text
        assert state.json()["workspace_id"] == ws_id, state.json()
        assert state.json()["messages"] == 2, state.json()
        assert state.json()["answer"] == "收到：你好"

        step("GET /chat/mine -> 只列本人的会话")
        mine = client.get("/chat/mine", headers=editor_h).json()["threads"]
        assert [t["thread_id"] for t in mine] == [thread_id], mine
        assert [t["workspace_id"] for t in mine] == [ws_id]
        assert client.get("/chat/mine", headers=outsider_h).json()["threads"] == []

        step("借别人的 thread_id -> 404（state / approve 都一样）")
        assert client.get(f"/chat/state/{thread_id}", headers=viewer_h).status_code == 404
        assert client.get(f"/chat/state/{thread_id}", headers=outsider_h).status_code == 404
        assert (
            client.post(
                "/chat/approve",
                json={"thread_id": thread_id, "decisions": [{"type": "approve"}]},
                headers=viewer_h,
            ).status_code
            == 404
        )
        assert client.get("/chat/state/nope", headers=editor_h).status_code == 404

        step("thread_id 含 ':' 或非法字符 -> 422")
        assert (
            client.post(
                "/chat/send",
                json={"workspace_id": ws_id, "message": "hi", "thread_id": f"{ROOT}:x"},
                headers=editor_h,
            ).status_code
            == 422
        )

        step("viewer 也能聊（聊天不受写限制）")
        resp = client.post(
            "/chat/send",
            json={"workspace_id": ws_id, "message": "在吗"},
            headers=viewer_h,
        )
        assert resp.status_code == 200 and resp.json()["answer"] == "收到：在吗", resp.text

        print("== /chat/* 人工批准闭环 ==")
        step("说「记住」-> interrupt，且记忆未落库")
        resp = client.post(
            "/chat/send",
            json={"workspace_id": ws_id, "message": "记住我喜欢简短回答"},
            headers=editor_h,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        approval_thread = body["thread_id"]
        assert body["answer"] == "" and body["interrupt"], body
        action = body["interrupt"]["action_requests"][0]
        assert action["name"] == "write_file", action
        assert action["args"]["file_path"] == "/memories/prefs.md", action
        assert (
            client.get("/memories/mine", params={"workspace_id": ws_id}, headers=editor_h)
            .json()["memories"]
            == []
        )

        step("POST /chat/approve -> 200，记忆落库")
        resp = client.post(
            "/chat/approve",
            json={"thread_id": approval_thread, "decisions": [{"type": "approve"}]},
            headers=editor_h,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["answer"] == "已记住" and resp.json()["interrupt"] is None, resp.json()
        assert (
            client.get("/memories/mine", params={"workspace_id": ws_id}, headers=editor_h)
            .json()["memories"]
            == ["/memories/prefs.md"]
        )

        step("approve 不接受请求体里的 workspace_id -> 422")
        assert (
            client.post(
                "/chat/approve",
                json={
                    "thread_id": approval_thread,
                    "decisions": [{"type": "approve"}],
                    "workspace_id": ws2_id,
                },
                headers=editor_h,
            ).status_code
            == 422
        )

        print("== SSE 流式 ==")
        step("/chat/stream -> token ... done，响应头带 X-Thread-Id")
        resp = client.post(
            "/chat/stream",
            json={"workspace_id": ws_id, "message": "流式你好"},
            headers=editor_h,
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("text/event-stream")
        stream_thread = resp.headers["x-thread-id"]
        kinds = [
            line[len("event: ") :]
            for line in resp.text.splitlines()
            if line.startswith("event: ")
        ]
        assert kinds[0] == "token" and kinds[-1] == "done", kinds
        assert "收到：流式你好" in resp.text

        step("流式会话也进了 /chat/mine")
        ids = [
            t["thread_id"]
            for t in client.get("/chat/mine", headers=editor_h).json()["threads"]
        ]
        assert stream_thread in ids, ids

        step("续聊同一个 thread_id -> 消息数增加")
        assert (
            client.post(
                "/chat/send",
                json={"workspace_id": ws_id, "message": "第二句", "thread_id": thread_id},
                headers=editor_h,
            ).status_code
            == 200
        )
        assert (
            client.get(f"/chat/state/{thread_id}", headers=editor_h).json()["messages"] == 4
        )

        print("== 短期记忆的读取与编辑 ==")
        step("GET /chat/history/{thread} -> 200，最旧→最新且带 id")
        resp = client.get(f"/chat/history/{thread_id}", headers=editor_h)
        assert resp.status_code == 200, resp.text
        messages = resp.json()["messages"]
        assert [m["role"] for m in messages] == ["human", "ai", "human", "ai"], messages
        assert messages[0]["content"] == "你好"

        step("POST /chat/messages/delete -> 204，消息数 -1，会话归属不丢")
        resp = client.post(
            "/chat/messages/delete",
            json={"thread_id": thread_id, "message_ids": [messages[-1]["id"]]},
            headers=editor_h,
        )
        assert resp.status_code == 204, resp.text
        after = client.get(f"/chat/state/{thread_id}", headers=editor_h)
        assert after.status_code == 200, after.text
        assert after.json()["messages"] == 3, after.json()
        assert after.json()["workspace_id"] == ws_id, after.json()

        step("删完还能继续聊")
        resp = client.post(
            "/chat/send",
            json={"workspace_id": ws_id, "message": "删完继续", "thread_id": thread_id},
            headers=editor_h,
        )
        assert resp.status_code == 200 and resp.json()["answer"] == "收到：删完继续", resp.text

        step("写个临时文件 -> /chat/state 的 files 里出现")
        assert (
            client.post(
                "/chat/send",
                json={"workspace_id": ws_id, "message": "临时笔记", "thread_id": thread_id},
                headers=editor_h,
            ).status_code
            == 200
        )
        files = client.get(f"/chat/state/{thread_id}", headers=editor_h).json()["files"]
        assert files == ["/tmp/note.txt"], files

        step("POST /chat/files/delete -> 204，files 里不再出现")
        resp = client.post(
            "/chat/files/delete",
            json={"thread_id": thread_id, "paths": ["/tmp/note.txt"]},
            headers=editor_h,
        )
        assert resp.status_code == 204, resp.text
        assert client.get(f"/chat/state/{thread_id}", headers=editor_h).json()["files"] == []

        step("借别人的 thread 删消息 / 删文件 / 删会话 -> 404")
        for path, body in (
            ("/chat/messages/delete", {"thread_id": thread_id, "message_ids": [messages[-1]["id"]]}),
            ("/chat/files/delete", {"thread_id": thread_id, "paths": ["/tmp/note.txt"]}),
        ):
            assert client.post(path, json=body, headers=viewer_h).status_code == 404, path
        assert client.delete(f"/chat/delete/{thread_id}", headers=viewer_h).status_code == 404

        step("DELETE /chat/delete/{thread} -> 204，之后 state 与 mine 都查不到")
        assert client.delete(f"/chat/delete/{thread_id}", headers=editor_h).status_code == 204
        assert client.get(f"/chat/state/{thread_id}", headers=editor_h).status_code == 404
        assert thread_id not in [
            t["thread_id"]
            for t in client.get("/chat/mine", headers=editor_h).json()["threads"]
        ]
        step("删会话不影响长期记忆")
        assert (
            client.get("/memories/mine", params={"workspace_id": ws_id}, headers=editor_h)
            .json()["memories"]
            == ["/memories/prefs.md"]
        )

    print(f"\n全部通过：{checks} 项检查")


if __name__ == "__main__":
    main()
