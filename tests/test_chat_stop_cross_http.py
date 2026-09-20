"""验证 ``/chat/stop`` 的**跨进程**行为：请求落到没有那一轮的进程上，也能停掉并拿到结果。

前置条件：本机 PostgreSQL 可连（``agents`` 库存在）、``user_related`` 已 ``alembic upgrade head``。
运行方式：``uv run python tests/test_chat_stop_cross_http.py``（自建自清）。

两个 ``create_app()`` 实例 = 两个 worker：``app.state.agent`` 是每实例一份的，所以它们
各有自己的连接池、自己的 ``TurnRegistry``、自己的 ``chat_drain`` 监听。两个 TestClient
跑在各自的线程里（各自的 event loop），比脚本级调用更接近真的多进程部署。

这里验的是**协议**（HTTP 层能走通的全链路）：停止请求打到 B、那一轮跑在 A 上、
A 通过 ``chat_drain`` 收到通知并收尾、B 等到结果返回 200。更细的分支
（心跳回收、孤儿恢复、重连补扫）在 ``test_chat_stop_cross.py`` 里用两个裸 agent 验。
"""

import asyncio
import sys
import threading
import time
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from langchain_core.language_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, AIMessageChunk  # noqa: E402
from langchain_core.outputs import (  # noqa: E402
    ChatGeneration,
    ChatGenerationChunk,
    ChatResult,
)

from agents.agent import GeneralAgent  # noqa: E402
from agents.config import AgentPostgreConfig  # noqa: E402
from config import app_config  # noqa: E402
from main import create_app  # noqa: E402

SUFFIX = uuid4().hex[:8]
ACCOUNT_FAMILY = "chat_stop_x_"
ACCOUNT = f"{ACCOUNT_FAMILY}{SUFFIX}"
PASSWORD = "Passw0rd!123"

checks = 0


def step(name: str) -> None:
    global checks
    checks += 1
    print(f"  [{checks:02d}] {name}")


class SlowModel(BaseChatModel):
    """真·流式假模型：逐字符吐，好在中途掐掉（不联真实 LLM）。"""

    CHUNKS: ClassVar[str] = "一二三四五六七八九十甲乙丙丁"
    DELAY: ClassVar[float] = 0.05

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return self

    @property
    def _llm_type(self) -> str:
        return "slow-stream"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return ChatResult(generations=[ChatGeneration(message=AIMessage("完整回答"))])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001, ANN003, ANN201
        for char in self.CHUNKS:
            await asyncio.sleep(self.DELAY)
            yield ChatGenerationChunk(message=AIMessageChunk(content=char))


def agent_db_execute(sql: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(AgentPostgreConfig().uri, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def db_execute(sql: str, params: tuple = ()) -> list[tuple]:
    """业务库（users 表在这）。"""
    uri = app_config.postgresql.user.sqlalchemy_uri.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(uri, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def cleanup() -> None:
    """按账号族清（thread_id 前缀是 user_id，不是账号名，所以先查 user_id）。"""
    like = f"{ACCOUNT_FAMILY}%"
    user_ids = [row[0] for row in db_execute("select id from users where account like %s", (like,))]
    for user_id in user_ids:
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            agent_db_execute(f"delete from {table} where thread_id like %s", (f"{user_id}:%",))
    agent_db_execute("delete from running_turns where thread_id like %s", (f"%{SUFFIX}%",))
    db_execute("delete from users where account like %s", (like,))


def headers(client: TestClient, account: str, create: bool) -> dict[str, str]:
    if create:
        resp = client.post(
            "/auth/register",
            json={"account": account, "email": f"{account}@example.com", "password": PASSWORD},
        )
        assert resp.status_code == 201, resp.text
    token = client.post(
        "/auth/login", json={"account": account, "password": PASSWORD}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def main() -> None:
    cleanup()
    try:
        run()
    finally:
        cleanup()
    print(f"\n全部通过：{checks} 项检查")


def run() -> None:
    GeneralAgent._build_model = lambda self: SlowModel()  # type: ignore[method-assign]

    # 两个 app = 两个 worker。各跑在自己的 TestClient 线程里，各有自己的 event loop。
    app_a, app_b = create_app(), create_app()
    with TestClient(app_a) as client_a, TestClient(app_b) as client_b:
        print("== 两个 worker（各自的 agent / registry / LISTEN 连接）==")

        # 注册只在 A 上做（同一份业务库，B 直接登录）
        account = f"{ACCOUNT_FAMILY}{SUFFIX}"
        headers_a = headers(client_a, account, create=True)
        headers_b = headers(client_b, account, create=False)
        print("-- C1 停止请求打到没有那一轮的 worker --")

        thread = f"t_xhttp_{SUFFIX}"
        box: dict = {}

        def stream() -> None:
            """在 A 上开一轮（模拟"这一轮跑在 A 这台 worker 上"）。"""
            try:
                box["resp"] = client_a.post(
                    "/chat/stream",
                    json={"message": "讲个长的", "thread_id": thread},
                    headers=headers_a,
                )
            except Exception as exc:  # noqa: BLE001
                box["error"] = exc  # 被停掉时流会断，预期之内

        runner = threading.Thread(target=stream)
        runner.start()
        time.sleep(0.5)  # 让 A 先吐出几个 chunk

        # 确认 B 上**没有**这一轮（否则这个测试就没验到跨进程）
        assert app_b.state.agent.turns.get(thread) is None, "B 不该有这一轮的本地登记"
        stop = client_b.post("/chat/stop", json={"thread_id": thread}, headers=headers_b)
        step(f"B 调 /chat/stop -> {stop.status_code}")
        assert stop.status_code == 200, stop.text
        answer = stop.json()["answer"]
        step(f"B 拿到的答案 {answer!r}")
        assert answer, "跨进程停止也要拿回答案"
        assert answer.startswith("一"), f"应该是 A 已流出的部分文本：{answer!r}"

        runner.join(timeout=10)
        assert not runner.is_alive(), "被停止后 A 上的流应该结束"

        # 历史由 A 收尾，但 B 也能读到（同一份检查点库）
        history = client_b.get(f"/chat/history/{thread}", headers=headers_b).json()["messages"]
        step(f"B 读到的历史 {[(m['role'], m['content']) for m in history]}")
        assert [m["role"] for m in history] == ["human", "ai"], (
            f"A 那一轮应该被就地收尾：{[(m['role'], m['content']) for m in history]}"
        )
        assert history[1]["content"] == answer, "历史里的文本要和 B 拿到的一致"

        step("登记行已被请求方清掉")
        assert agent_db_execute(
            "select 1 from running_turns where thread_id = %s", (thread,)
        ) == []

        print("-- C2 停止之后，另一个 worker 能接着开这一轮 --")
        again = client_b.post(
            "/chat/send", json={"message": "接着聊", "thread_id": thread}, headers=headers_b
        )
        step(f"B 上 /chat/send -> {again.status_code}")
        assert again.status_code == 200, again.text
        history = client_b.get(f"/chat/history/{thread}", headers=headers_b).json()["messages"]
        assert [m["role"] for m in history] == ["human", "ai", "human", "ai"], (
            f"停止之后要能续聊，不能合并：{[(m['role'], m['content']) for m in history]}"
        )

        print("-- C3 A 在跑时，B 上开同一会话 -> 409（跨进程互斥）--")
        thread = f"t_xbusy_{SUFFIX}"
        box.clear()

        def stream2() -> None:
            try:
                box["resp"] = client_a.post(
                    "/chat/stream",
                    json={"message": "占着", "thread_id": thread},
                    headers=headers_a,
                )
            except Exception as exc:  # noqa: BLE001
                box["error"] = exc

        runner2 = threading.Thread(target=stream2)
        runner2.start()
        time.sleep(0.5)
        busy = client_b.post(
            "/chat/send", json={"message": "插队", "thread_id": thread}, headers=headers_b
        )
        step(f"B 上并发发一条 -> {busy.status_code}（A 在跑）")
        assert busy.status_code == 409, busy.text

        stop = client_b.post("/chat/stop", json={"thread_id": thread}, headers=headers_b)
        assert stop.status_code == 200, stop.text
        runner2.join(timeout=10)
        assert not runner2.is_alive()

        step("停掉之后 B 能开这一轮了（互斥已释放）")
        assert (
            client_b.post(
                "/chat/send", json={"message": "现在可以了", "thread_id": thread},
                headers=headers_b,
            ).status_code
            == 200
        )

        print("-- C4 没人在跑时按停止 -> 200 幂等，不靠超时 --")
        thread = f"t_xdone_{SUFFIX}"
        sent = client_a.post(
            "/chat/send", json={"message": "你好", "thread_id": thread}, headers=headers_a
        )
        assert sent.status_code == 200, sent.text
        began = time.time()
        # 打到 B（没有这一轮），但此时 A 也已经跑完了 -> request_stop 返回 None -> 直接收尾
        stopped = client_b.post("/chat/stop", json={"thread_id": thread}, headers=headers_b)
        elapsed = time.time() - began
        step(f"跑完的会话在 B 上按停止 -> {stopped.status_code}，耗时 {elapsed:.2f}s")
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["answer"] == sent.json()["answer"], "幂等：返回原来的回答"
        assert elapsed < 2.0, "没人在跑就不该等超时（一发现表里没行就该返回）"

        before = client_b.get(f"/chat/history/{thread}", headers=headers_b).json()["messages"]
        again = client_b.post("/chat/stop", json={"thread_id": thread}, headers=headers_b)
        after = client_b.get(f"/chat/history/{thread}", headers=headers_b).json()["messages"]
        assert again.status_code == 200 and after == before, "重复按不污染历史"

        print("-- C5 别人的会话 -> 404（跨 worker 也不泄露存在性）--")
        other = f"{ACCOUNT_FAMILY}other_{SUFFIX}"
        headers_other = headers(client_b, other, create=True)
        nope = client_b.post(
            "/chat/stop", json={"thread_id": thread}, headers=headers_other
        )
        step(f"陌生人按别人的会话 -> {nope.status_code}")
        assert nope.status_code == 404, nope.text


if __name__ == "__main__":
    main()