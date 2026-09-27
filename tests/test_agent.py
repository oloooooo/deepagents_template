"""deepagents 端到端验证：PostgreSQL 持久化 state（短时记忆）+ store（长期记忆 / 文件后端）。

前置条件：本机 PostgreSQL 可用 + 环境变量里有 DEEPSEEK_API_KEY（会真实调用模型）。
运行方式：``uv run python tests/test_agent.py``（无需 pytest，失败即非 0 退出）。

覆盖点：
1. ``DeepAgent`` 的 aenter/aexit 生命周期与未启动时的守卫；
2. ``ainvoke`` 的回答写进 PostgreSQL 检查点，**换一个实例（新连接池）仍能续聊**；
3. ``astream`` 逐 token 输出，并以 ``done`` 事件给出完整回答；
4. ``/memories/`` 文件落在 store 里，**换一个 thread 也能读回**，且用户之间命名空间隔离；
5. ``/agent/*`` 路由必须带用户 access token（401），并通过 HTTP 复用同一份持久化。
"""

import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agents import DeepAgent  # noqa: E402
from config import app_config  # noqa: E402
from main import app  # noqa: E402

SUFFIX = uuid4().hex[:8]
USER = f"agent-{SUFFIX}"  # 直接调类时使用的用户 id（同时作为 store 命名空间与 thread 前缀）
OTHER = f"agent-other-{SUFFIX}"
ACCOUNT = f"agent_{SUFFIX}"
EMAIL = f"agent_{SUFFIX}@example.com"
PASSWORD = "Passw0rd!123"
CODENAME = "夜枭"

checks = 0


def step(name: str) -> None:
    global checks
    checks += 1
    print(f"  [{checks:02d}] {name}")


def db_rows(sql: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(app_config.postgresql.deepagent.uri, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def cleanup() -> None:
    """删掉本次测试留下的用户与持久化数据（thread_id 与 store 命名空间都带用户 id 前缀）。"""
    accounts = [ACCOUNT, f"agent_other_{SUFFIX}"]
    ids = [row[0] for row in db_rows("select id from users where account = any(%s)", (accounts,))]
    for prefix in (USER, OTHER, *ids):
        pattern = f"{prefix}:%"
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            db_rows(f"delete from {table} where thread_id like %s", (pattern,))
        db_rows("delete from store where prefix like %s", (f"{prefix}%",))
    db_rows("delete from users where account = any(%s)", (accounts,))


def parse_sse(body: str) -> list[tuple[str, dict]]:
    """把 SSE 文本解析成 [(event, data), ...]。"""
    events: list[tuple[str, dict]] = []
    for block in body.strip().split("\n\n"):
        event, data = "message", ""
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        if event != "message":
            events.append((event, {} if data == "[DONE]" else json.loads(data)))
    return events


# ---------------------------------------------------------------- 直接使用类


async def class_level_checks() -> None:
    print("== DeepAgent 生命周期 ==")
    step("未启动时访问 graph / store 会抛 RuntimeError（避免悄悄跑成无持久化）")
    idle = DeepAgent()
    for attr in ("graph", "store"):
        try:
            getattr(idle, attr)
            raise AssertionError(f"未启动就能访问 {attr}")
        except RuntimeError:
            pass

    step("aenter()/aexit() 显式方法可用，退出后再次访问 graph 抛错")
    manual = DeepAgent()
    await manual.aenter()
    assert manual.graph is not None and manual.store is not None
    await manual.aexit()
    try:
        manual.graph
        raise AssertionError("aexit 之后 graph 仍可访问")
    except RuntimeError:
        pass

    print("== 短时记忆：state 落库 + 跨实例续聊 ==")
    step("ainvoke 一轮并回答")
    async with DeepAgent() as agent:
        answer = await agent.ainvoke(
            f"记住：我的代号是{CODENAME}。只回复两个字：收到", thread_id="t-mem", user_id=USER
        )
        assert answer.strip(), "回答为空"
        checkpoint_rows = db_rows(
            "select count(*) from checkpoints where thread_id = %s", (f"{USER}:t-mem",)
        )[0][0]
        assert checkpoint_rows > 0, "检查点里没有该会话"
    print(f"       回答={answer.strip()[:20]!r}，checkpoints 行数={checkpoint_rows}")

    step("换一个全新实例（新连接池）读同一会话：消息条数 > 0")
    async with DeepAgent() as agent:
        state = await agent.aget_state("t-mem", USER)
        assert state["messages"] > 0, state
        assert state["conversation_id"] == f"{USER}:t-mem"

        step("跨实例继续同一会话，模型记得代号")
        answer = await agent.ainvoke(
            "我的代号是什么？只回复代号", thread_id="t-mem", user_id=USER
        )
        assert CODENAME in answer, f"没记住代号: {answer!r}"
        print(f"       回答={answer.strip()[:20]!r}")

    print("== 流式输出 ==")
    step("astream 产出 token 事件，并以 done 收尾（done.text 与检查点一致）")
    async with DeepAgent() as agent:
        tokens, done_text, kinds = 0, "", []
        async for event in agent.astream("用一句话介绍你自己", thread_id="t-stream", user_id=USER):
            kinds.append(event.kind)
            tokens += len(event.text) if event.kind == "token" else 0
            if event.kind == "done":
                done_text = event.text
        assert "token" in kinds and kinds[-1] == "done", kinds
        assert tokens > 0, "没有收到任何 token"
        assert done_text, "done 事件没有带完整回答"
        state = await agent.aget_state("t-stream", USER)
        assert state["answer"] == done_text, "done 与检查点里的回答不一致"
    print(f"       token 字符数={tokens}，事件序列尾部={kinds[-3:]}")

    print("== 长期记忆：store + /memories/ 文件后端 ==")
    step("把内容写进 /memories/profile.md")
    async with DeepAgent() as agent:
        await agent.ainvoke(
            f"请把 '代号={CODENAME}；偏好=中文' 写入 /memories/profile.md，然后只回复 OK",
            thread_id="t-mem",
            user_id=USER,
        )
        memories = await agent.alist_memories(USER)
        assert memories, "store 里没有文件"
        store_rows = db_rows("select count(*) from store where prefix like %s", (f"{USER}%",))[0][0]
        assert store_rows > 0, "store 表里没有该用户的记录"
    print(f"       store 文件={memories}，store 行数={store_rows}")

    step("换个 thread（新会话）+ 新实例，仍能从 /memories/ 读回长期记忆")
    async with DeepAgent() as agent:
        answer = await agent.ainvoke(
            "读取 /memories/profile.md 的内容并原样回复，不要解释", thread_id="t-other", user_id=USER
        )
        assert CODENAME in answer, f"长期记忆没读到: {answer!r}"
        print(f"       回答={answer.strip()[:40]!r}")

    step("另一个用户的 store 命名空间是空的（互不可见）")
    async with DeepAgent() as agent:
        assert await agent.alist_memories(OTHER) == []


# ---------------------------------------------------------------- HTTP 路由


def router_checks() -> None:
    with TestClient(app) as client:
        print("== 用户 access token 校验 ==")
        step("注册并登录测试用户，拿到 access token")
        resp = client.post(
            "/auth/register", json={"account": ACCOUNT, "email": EMAIL, "password": PASSWORD}
        )
        assert resp.status_code == 201, resp.text
        user_id = resp.json()["id"]
        resp = client.post("/auth/login", json={"account": ACCOUNT, "password": PASSWORD})
        assert resp.status_code == 200, resp.text
        token = resp.json()["access_token"]
        auth = {"Authorization": f"Bearer {token}"}

        step("不带 token 调 /agent/chat、/agent/state、/agent/memories、/agent/stream → 401")
        for method, url in (
            ("post", "/agent/chat"),
            ("post", "/agent/stream"),
            ("get", "/agent/state/t1"),
            ("get", "/agent/memories"),
        ):
            kwargs = {"json": {"message": "hi"}} if method == "post" else {}
            resp = getattr(client, method)(url, **kwargs)
            assert resp.status_code == 401, f"{url} -> {resp.status_code}"
        step("token 非法（伪造签名）→ 401")
        assert client.post(
            "/agent/chat",
            json={"message": "hi"},
            headers={"Authorization": f"Bearer {token[:-4]}beef"},
        ).status_code == 401

        print("== 对话（一次性返回） ==")
        step("带 token 调 /agent/chat → 200 且回答非空")
        resp = client.post("/agent/chat", json={"message": "只回复四个字：你好世界", "thread_id": "chat"}, headers=auth)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["thread_id"] == "chat" and body["answer"].strip(), body
        print(f"       answer={body['answer'].strip()[:30]!r}")

        step("检查点里确实写了该用户 + thread（HTTP 路径同样落库）")
        rows = db_rows(
            "select count(*) from checkpoints where thread_id = %s", (f"{user_id}:chat",)
        )[0][0]
        assert rows > 0, "HTTP 对话没有写检查点"

        step("GET /agent/state/{thread_id} → 短时记忆（消息数 > 0）")
        resp = client.get("/agent/state/chat", headers=auth)
        assert resp.status_code == 200, resp.text
        state = resp.json()
        assert state["user_id"] == user_id and state["messages"] > 0 and state["answer"], state

        step("别人的 token 查同一个 thread_id 拿不到内容（thread 按用户隔离）")
        other_account = f"agent_other_{SUFFIX}"
        resp = client.post(
            "/auth/register",
            json={"account": other_account, "email": f"{other_account}@example.com", "password": PASSWORD},
        )
        assert resp.status_code == 201, resp.text
        other_token = client.post(
            "/auth/login", json={"account": other_account, "password": PASSWORD}
        ).json()["access_token"]
        resp = client.get("/agent/state/chat", headers={"Authorization": f"Bearer {other_token}"})
        assert resp.status_code == 200 and resp.json()["messages"] == 0, resp.text

        print("== 流式对话（SSE） ==")
        step("POST /agent/stream → text/event-stream，含 token 事件与 done 事件")
        resp = client.post(
            "/agent/stream", json={"message": "数一下：1 加 1 等于几？只回复数字", "thread_id": "stream"}, headers=auth
        )
        assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
        events = parse_sse(resp.text)
        kinds = [kind for kind, _ in events]
        assert "token" in kinds and "done" in kinds and kinds[-1] == "end", kinds
        done = next(data for kind, data in events if kind == "done")
        assert done["text"] and done["thread_id"] == "stream" and done["messages"] > 0, done
        print(f"       SSE 事件数={len(events)}，done.text={done['text'].strip()[:30]!r}")


def main() -> None:
    cleanup()
    try:
        asyncio.run(class_level_checks())
        router_checks()
        print("== 清理 ==")
        step("删除测试用户与其 checkpoints / store 数据（HTTP 用户与直接调类的用户都清）")
        cleanup()
        assert db_rows(
            "select count(*) from checkpoints where thread_id like %s", (f"{USER}:%",)
        )[0][0] == 0
        assert db_rows("select count(*) from store where prefix like %s", (f"{USER}%",))[0][0] == 0
        assert db_rows("select count(*) from users where account = %s", (ACCOUNT,))[0][0] == 0
    finally:
        cleanup()
    print(f"全部通过：{checks} 项检查")


if __name__ == "__main__":
    main()
