"""Agent 相关 HTTP 路由的端到端验证（/memories/* 与 /chat/*）。

前置条件：本机 PostgreSQL 可连（``user_related`` 已 ``alembic upgrade head``；``agents`` 库存在）
且 ``.env`` 里有模型配置 —— lifespan 会起 ``GeneralAgent``，但假模型保证不发网络请求。
运行方式：``uv run python tests/test_agent_api.py``（自建自清）。

覆盖：记忆库的写/读/列/删契约、按用户隔离、请求体塞 user_id / workspace_id 被拒、
路径穿越被拒、聊天契约、人工批准闭环、SSE 流式、短期记忆编辑。
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
ALICE, BOB, OUTSIDER = (
    f"{ACCOUNT_FAMILY}al_{SUFFIX}",
    f"{ACCOUNT_FAMILY}bo_{SUFFIX}",
    f"{ACCOUNT_FAMILY}out_{SUFFIX}",
)

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
    """业务库（users）。"""
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
    db_execute("delete from users where account like %s", (like,))


def main() -> None:
    cleanup()
    GeneralAgent._build_model = lambda self: SCRIPTED_MODEL  # type: ignore[method-assign]
    with TestClient(app) as client:
        print("== 准备：注册 alice / bob / 陌生人 ==")
        for account in (ALICE, BOB, OUTSIDER):
            resp = client.post(
                "/auth/register",
                json={"account": account, "email": f"{account}@example.com", "password": PASSWORD},
            )
            assert resp.status_code == 201, resp.text

        def headers(account: str) -> dict[str, str]:
            token = client.post(
                "/auth/login", json={"account": account, "password": PASSWORD}
            ).json()["access_token"]
            return {"Authorization": f"Bearer {token}"}

        alice_h, bob_h, outsider_h = headers(ALICE), headers(BOB), headers(OUTSIDER)

        print("== /memories/* 契约 ==")
        step("未登录 GET /memories/all -> 401")
        assert client.get("/memories/all").status_code == 401

        step("alice POST /memories/write -> 200 且返回落库路径（agent 能直接用）")
        resp = client.post(
            "/memories/write",
            json={"path": "prefs.md", "content": "喜欢简短回答"},
            headers=alice_h,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"path": "/memories/prefs.md"}, resp.json()

        step("alice POST /memories/read -> 200 且内容一致（/memories/ 前缀写法也收）")
        resp = client.post(
            "/memories/read",
            json={"path": "/memories/prefs.md"},
            headers=alice_h,
        )
        assert resp.status_code == 200 and resp.json() == {
            "path": "/memories/prefs.md",
            "content": "喜欢简短回答",
        }, resp.text

        step("alice GET /memories/all -> 200 且列出该文件")
        resp = client.get("/memories/all", headers=alice_h)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"memories": ["/memories/prefs.md"]}, resp.json()

        step("覆写同一路径 -> 200 且内容更新")
        resp = client.post(
            "/memories/write",
            json={"path": "prefs.md", "content": "改成详细回答"},
            headers=alice_h,
        )
        assert resp.status_code == 200, resp.text
        assert (
            client.post(
                "/memories/read",
                json={"path": "prefs.md"},
                headers=alice_h,
            ).json()["content"]
            == "改成详细回答"
        )

        print("== /memories/* 归属与校验 ==")
        step("bob 看不到 alice 的记忆：list 为空，read -> 404")
        assert client.get("/memories/all", headers=bob_h).json() == {"memories": []}
        assert (
            client.post(
                "/memories/read",
                json={"path": "prefs.md"},
                headers=bob_h,
            ).status_code
            == 404
        )
        step("bob 删 alice 的记忆 -> 404（互相看不见，也删不掉）")
        assert (
            client.post(
                "/memories/delete",
                json={"path": "prefs.md"},
                headers=bob_h,
            ).status_code
            == 404
        )

        step("请求体里塞 user_id -> 422（归属只认登录态）")
        resp = client.post(
            "/memories/write",
            json={"path": "sneak.md", "content": "x", "user_id": ALICE},
            headers=bob_h,
        )
        assert resp.status_code == 422, resp.text
        step("请求体里塞 workspace_id -> 422（空间维度已移除）")
        resp = client.post(
            "/memories/write",
            json={"path": "sneak.md", "content": "x", "workspace_id": "default"},
            headers=alice_h,
        )
        assert resp.status_code == 422, resp.text
        step("路径穿越 / 空路径 -> 422")
        for bad in ("../../etc/passwd", "~/.ssh/id_rsa", "/"):
            resp = client.post(
                "/memories/write",
                json={"path": bad, "content": "x"},
                headers=alice_h,
            )
            assert resp.status_code == 422, (bad, resp.text)

        print("== 删除 ==")
        step("alice POST /memories/delete -> 204")
        resp = client.post("/memories/delete", json={"path": "prefs.md"}, headers=alice_h)
        assert resp.status_code == 204, resp.text
        step("再读 -> 404，再删 -> 404")
        assert (
            client.post("/memories/read", json={"path": "prefs.md"}, headers=alice_h).status_code
            == 404
        )
        assert (
            client.post("/memories/delete", json={"path": "prefs.md"}, headers=alice_h).status_code
            == 404
        )
        step("库里确实没留下记忆")
        alice_id = db_execute("select id from users where account = %s", (ALICE,))[0][0]
        assert (
            agent_db_execute(
                "select count(*) from store where prefix like %s", (f"{alice_id}.%",)
            )
            == [(0,)]
        )

        print("== /memories/upload ==")
        step("alice 上传两份（一份文本、一份二进制）-> 200，逐份结果，坏的不影响好的")
        resp = client.post(
            "/memories/upload",
            files=[
                ("files", ("notes.md", "上传的笔记".encode(), "text/markdown")),
                ("files", ("raw.bin", b"\xff\xfe\x00\x01", "application/octet-stream")),
            ],
            headers=alice_h,
        )
        assert resp.status_code == 200, resp.text
        results = resp.json()["results"]
        assert results[0] == {
            "file": "notes.md",
            "path": "/memories/notes.md",
            "error": None,
        }, results
        assert results[1]["path"] is None and "文本" in results[1]["error"], results

        step("上传的内容能读回来，也进 /memories/all")
        assert (
            client.post(
                "/memories/read",
                json={"path": "notes.md"},
                headers=alice_h,
            ).json()["content"]
            == "上传的笔记"
        )
        assert client.get("/memories/all", headers=alice_h).json() == {
            "memories": ["/memories/notes.md"]
        }
        step("bob 的 /memories/all 仍然为空（上传也按用户隔离）")
        assert client.get("/memories/all", headers=bob_h).json() == {"memories": []}

        print("== /chat/* 契约 ==")
        step("未登录 POST /chat/send -> 401")
        assert client.post("/chat/send", json={"message": "hi"}).status_code == 401

        step("alice POST /chat/send -> 200，返回 thread_id 与回答")
        resp = client.post("/chat/send", json={"message": "你好"}, headers=alice_h)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        thread_id = body["thread_id"]
        assert body["answer"] == "收到：你好" and body["interrupt"] is None, body

        step("GET /chat/state/{thread} -> 200，带消息数")
        state = client.get(f"/chat/state/{thread_id}", headers=alice_h)
        assert state.status_code == 200, state.text
        assert state.json()["messages"] == 2, state.json()
        assert state.json()["answer"] == "收到：你好"

        step("GET /chat/mine -> 只列本人的会话")
        mine = client.get("/chat/mine", headers=alice_h).json()["threads"]
        assert [t["thread_id"] for t in mine] == [thread_id], mine
        assert client.get("/chat/mine", headers=outsider_h).json()["threads"] == []

        step("借别人的 thread_id -> 404（state / approve 都一样）")
        assert client.get(f"/chat/state/{thread_id}", headers=bob_h).status_code == 404
        assert client.get(f"/chat/state/{thread_id}", headers=outsider_h).status_code == 404
        assert (
            client.post(
                "/chat/approve",
                json={"thread_id": thread_id, "decisions": [{"type": "approve"}]},
                headers=bob_h,
            ).status_code
            == 404
        )
        assert client.get("/chat/state/nope", headers=alice_h).status_code == 404

        step("thread_id 含 ':' 或非法字符 -> 422")
        assert (
            client.post(
                "/chat/send",
                json={"message": "hi", "thread_id": f"{ALICE}:x"},
                headers=alice_h,
            ).status_code
            == 422
        )
        step("请求体里塞 workspace_id -> 422（空间维度已移除）")
        assert (
            client.post(
                "/chat/send",
                json={"message": "hi", "workspace_id": "default"},
                headers=alice_h,
            ).status_code
            == 422
        )

        print("== /chat/* 人工批准闭环 ==")
        step("说「记住」-> interrupt，且记忆未落库")
        resp = client.post(
            "/chat/send",
            json={"message": "记住我喜欢简短回答"},
            headers=alice_h,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        approval_thread = body["thread_id"]
        assert body["answer"] == "" and body["interrupt"], body
        action = body["interrupt"]["action_requests"][0]
        assert action["name"] == "write_file", action
        assert action["args"]["file_path"] == "/memories/prefs.md", action
        assert client.get("/memories/all", headers=alice_h).json() == {
            "memories": ["/memories/notes.md"]
        }

        step("POST /chat/approve -> 200，记忆落库")
        resp = client.post(
            "/chat/approve",
            json={"thread_id": approval_thread, "decisions": [{"type": "approve"}]},
            headers=alice_h,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["answer"] == "已记住" and resp.json()["interrupt"] is None, resp.json()
        step("批准后记忆进了 alice 自己的清单")
        assert client.get("/memories/all", headers=alice_h).json() == {
            "memories": ["/memories/notes.md", "/memories/prefs.md"]
        }

        step("approve 不接受请求体里的 user_id -> 422")
        assert (
            client.post(
                "/chat/approve",
                json={
                    "thread_id": approval_thread,
                    "decisions": [{"type": "approve"}],
                    "user_id": BOB,
                },
                headers=alice_h,
            ).status_code
            == 422
        )

        print("== SSE 流式 ==")
        step("/chat/stream -> token ... done，响应头带 X-Thread-Id")
        resp = client.post("/chat/stream", json={"message": "流式你好"}, headers=alice_h)
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
            t["thread_id"] for t in client.get("/chat/mine", headers=alice_h).json()["threads"]
        ]
        assert stream_thread in ids, ids

        step("续聊同一个 thread_id -> 消息数增加")
        assert (
            client.post(
                "/chat/send",
                json={"message": "第二句", "thread_id": thread_id},
                headers=alice_h,
            ).status_code
            == 200
        )
        assert (
            client.get(f"/chat/state/{thread_id}", headers=alice_h).json()["messages"] == 4
        )

        print("== 短期记忆的读取与编辑 ==")
        step("GET /chat/history/{thread} -> 200，最旧→最新且带 id")
        resp = client.get(f"/chat/history/{thread_id}", headers=alice_h)
        assert resp.status_code == 200, resp.text
        messages = resp.json()["messages"]
        assert [m["role"] for m in messages] == ["human", "ai", "human", "ai"], messages
        assert messages[0]["content"] == "你好"

        step("POST /chat/messages/delete -> 204，消息数 -1，会话归属不丢")
        resp = client.post(
            "/chat/messages/delete",
            json={"thread_id": thread_id, "message_ids": [messages[-1]["id"]]},
            headers=alice_h,
        )
        assert resp.status_code == 204, resp.text
        after = client.get(f"/chat/state/{thread_id}", headers=alice_h)
        assert after.status_code == 200, after.text
        assert after.json()["messages"] == 3, after.json()

        step("删完还能继续聊")
        resp = client.post(
            "/chat/send",
            json={"message": "删完继续", "thread_id": thread_id},
            headers=alice_h,
        )
        assert resp.status_code == 200 and resp.json()["answer"] == "收到：删完继续", resp.text

        step("写个临时文件 -> /chat/state 的 files 里出现")
        assert (
            client.post(
                "/chat/send",
                json={"message": "临时笔记", "thread_id": thread_id},
                headers=alice_h,
            ).status_code
            == 200
        )
        files = client.get(f"/chat/state/{thread_id}", headers=alice_h).json()["files"]
        assert files == ["/tmp/note.txt"], files

        step("POST /chat/files/delete -> 204，files 里不再出现")
        resp = client.post(
            "/chat/files/delete",
            json={"thread_id": thread_id, "paths": ["/tmp/note.txt"]},
            headers=alice_h,
        )
        assert resp.status_code == 204, resp.text
        assert client.get(f"/chat/state/{thread_id}", headers=alice_h).json()["files"] == []

        step("借别人的 thread 删消息 / 删文件 / 删会话 -> 404")
        for path, body in (
            ("/chat/messages/delete", {"thread_id": thread_id, "message_ids": [messages[-1]["id"]]}),
            ("/chat/files/delete", {"thread_id": thread_id, "paths": ["/tmp/note.txt"]}),
        ):
            assert client.post(path, json=body, headers=bob_h).status_code == 404, path
        assert client.delete(f"/chat/delete/{thread_id}", headers=bob_h).status_code == 404

        step("DELETE /chat/delete/{thread} -> 204，之后 state 与 mine 都查不到")
        assert client.delete(f"/chat/delete/{thread_id}", headers=alice_h).status_code == 204
        assert client.get(f"/chat/state/{thread_id}", headers=alice_h).status_code == 404
        assert thread_id not in [
            t["thread_id"] for t in client.get("/chat/mine", headers=alice_h).json()["threads"]
        ]
        step("删会话不影响长期记忆")
        assert client.get("/memories/all", headers=alice_h).json() == {
            "memories": ["/memories/notes.md", "/memories/prefs.md"]
        }

        step("bob 有自己的记忆和会话，跟 alice 互不可见")
        assert client.post(
            "/memories/write",
            json={"path": "bob.md", "content": "bob 的偏好"},
            headers=bob_h,
        ).status_code == 200
        resp = client.post("/chat/send", json={"message": "bob 在聊"}, headers=bob_h)
        assert resp.status_code == 200, resp.text
        bob_thread = resp.json()["thread_id"]
        assert client.get("/memories/all", headers=alice_h).json() == {
            "memories": ["/memories/notes.md", "/memories/prefs.md"]
        }
        assert client.get(f"/chat/state/{thread_id}", headers=bob_h).status_code == 404
        assert [t["thread_id"] for t in client.get("/chat/mine", headers=bob_h).json()["threads"]] == [
            bob_thread
        ]

    print(f"\n全部通过：{checks} 项检查")


if __name__ == "__main__":
    main()
