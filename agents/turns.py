"""跨进程的「在跑的轮次」：一张表 + 一条 LISTEN 长连接。

停止请求可能落到**任何**一个 worker，而能执行取消的只有**持有那个 task 的进程**——
``asyncio.Task`` 是运行时对象，进不了数据库。所以这件事拆成两半（见 ``docs/adr/0004`` / ``0006``）：

- **意图与归属**：``running_turns`` 表，跨进程共享。谁在跑、要不要停、结果是什么都在这；
- **执行能力**：:class:`TurnRegistry`，只在本进程。那个 ``Task`` 和已流出的文本在这。

投递走 Postgres 的 ``LISTEN/NOTIFY``：任何一个 worker 收到 ``/chat/stop`` 都往通道里喊一声，
**所有** worker 都收得到，只有认领那一条的那个会动手 —— 所以不需要负载均衡器做会话粘性。

两个已知的代价（写在这免得后来人当成 bug）：

- **通知是即发即弃的**：不在监听时发出的会丢（实测），所以重连之后要补扫一遍表；
- **worker 崩了没人收尾**：表里那行还在，但没人认领，请求方会等满超时再兜底。
  根治在下一轮开轮次时的孤儿恢复（:meth:`ChatService.open_turn` 那块）。
"""

import asyncio
import os
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import psycopg

from logger import logger

__all__ = [
    "CHANNEL",
    "STOP_TIMEOUT",
    "TURN_HEARTBEAT_TTL",
    "RunningTurns",
    "StopRequest",
    "Turn",
    "TurnRegistry",
    "worker_id",
]

CHANNEL = "chat_drain"
"""LISTEN/NOTIFY 的通道名。payload 是 thread_id。"""

STOP_TIMEOUT = 5.0
"""等被取消的那一轮真正停下来，最多等这么久（秒）。"""

TURN_HEARTBEAT_TTL = timedelta(seconds=60)
"""多久没心跳就当那个 worker 挂了，把那行清掉。

listener 每 :data:`SWEEP_INTERVAL` 给自己在跑的轮次打一次心跳，所以这个值要明显大于它
（现在 = 12 个周期）。太短会在 listener 重连时误伤还在跑的轮次（那就丢掉互斥了），
太长则 worker 挂掉后那个会话要干等很久才能再发消息。
"""

SWEEP_INTERVAL = 5.0
"""listener 的空闲周期（秒）。每次空闲顺手清一遍陈行，顺带当作心跳。"""

POLL_INTERVAL = 0.05
"""请求方等 owner 收尾的轮询间隔（秒）。只在一个停止请求进行中才跑。"""


def worker_id() -> str:
    """本进程的标识。

    只用来区分「我」和「别人」（以及日志好认），不参与寻址 —— ``NOTIFY`` 是广播的，
    不需要知道谁是谁。
    """
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass(slots=True)
class Turn:
    """一轮正在跑的对话（记录在 :class:`TurnRegistry` 里，停止时要用）。

    ``text`` 是**已流出的增量文本**的累加：它不落检查点（AI 消息只在超步结束时才写），
    所以停止时必须靠它把用户已经看到的字写回历史。

    ``user_id`` 也放在这里，是为了让收到停止通知的那个进程**不再查库**
    就能拼出 ``{user_id}:{thread_id}`` 去收尾 —— 它在收到通知的那一刻只有 ``thread_id``。
    """

    thread_id: str
    user_id: str
    task: asyncio.Task[Any] | None = None
    """跑这一轮的那个 task，由服务层绑上（``None`` = 还没开始跑）。停止时取消它。"""
    text: str = ""


class TurnRegistry:
    """**本进程**正在跑的轮次：``thread_id -> Turn``。

    故意挂在 :class:`GeneralAgent` 上而不是做成模块级单例：生命周期跟着 agent（进程）走，
    测试里每建一个 agent 就是一份干净的表。

    互斥已经交给 ``running_turns`` 表（跨进程），所以这里只管两件事：**找 task** 和 **攒文本**。
    这两个都只在内存里 —— 详见 ``docs/adr/0004``。
    """

    def __init__(self) -> None:
        self._turns: dict[str, Turn] = {}

    def get(self, thread_id: str) -> Turn | None:
        return self._turns.get(thread_id)

    def reserve(self, thread_id: str, user_id: str) -> Turn:
        """占一个位置**（调用方必须先拿到 ``running_turns`` 表的行，那张表才是互斥）**。

        **同步、无 await**：调用方必须在任何 await 之前调它，否则两个并发请求会双双通过检查。
        顺手把 ``task`` 绑成当前任务（阻塞式那一轮就是当前请求任务；流式会被
        ``ChatService._tracked`` 改成真正跑迭代的子任务）。

        顺带自愈：上一轮的 task 已经结束却还赖在表里（客户端在响应体开始前就断了，
        生成器的 ``finally`` 因此没跑到），就把它当陈的挤掉。
        """
        existing = self._turns.get(thread_id)
        if existing is not None and existing.task is not None and not existing.task.done():
            raise RuntimeError(f"这一轮已经在跑了：{thread_id}")
        turn = Turn(
            thread_id=thread_id,
            user_id=user_id,
            task=asyncio.current_task(),
        )
        self._turns[thread_id] = turn
        return turn

    def release(self, turn: Turn) -> None:
        """释放。只在自己还是当前持有者时才删，不误删后来者的。"""
        if self._turns.get(turn.thread_id) is turn:
            del self._turns[turn.thread_id]

    def names(self) -> list[str]:
        """本进程正在跑的 thread_id（日志用）。"""
        return list(self._turns)


@dataclass(frozen=True, slots=True)
class StopRequest:
    """一次停止请求落地后的样子。"""

    owner: str
    """在跑那一轮的 worker_id。可能是自己（调用方应该走本地快路径，不会走到这）。"""
    answer: str | None = None
    """非空 = owner 已经收尾完了，直接用它，不用再等。"""


class RunningTurns:
    """``running_turns`` 表的读写 + ``chat_drain`` 通道的收发。

    由 :class:`GeneralAgent` 在 ``__aenter__`` 里建、``__aexit__`` 里关，跟着进程的生命周期。

    **两条专用连接**，都不进连接池：

    - 一条 ``LISTEN``（LISTEN 是连接级状态，借来的连接会污染）；
    - 一条跑控制查询，用 :attr:`_lock` 串行化。

    连接预算要按 ``workers × 2`` 多留两个位置（见 README 的连接数那一段）。
    """

    def __init__(self, uri: str, on_stop: Callable[[str, Turn], Awaitable[None]]) -> None:
        self.uri = uri
        self.worker = worker_id()
        self._on_stop = on_stop
        self._registry: TurnRegistry | None = None
        self._control: psycopg.AsyncConnection | None = None
        self._listener_conn: psycopg.AsyncConnection | None = None
        """当前那条 LISTEN 连接。测试靠它注入断线（正常代码不碰）。"""
        self._listener: asyncio.Task[None] | None = None
        self._dispatches: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()

    # ---------- 生命周期 ----------

    async def start(self, registry: TurnRegistry) -> None:
        """开控制连接、起 listener、幂等建表。

        建表用 ``create table if not exists``，和 langgraph 自己的 ``checkpoints`` / ``store``
        一致 —— 这三个表都在 **agents 库**（不是 alembic 管的 ``user_related`` 库），
        那个库就是靠各家的幂等 ``setup()`` 管起来的。

        ponytail: 将来要给这张表**加列**，``if not exists`` 会静静不生效（表已存在），
        得像 langgraph 那样上一个 migrations 列表。现在只有这一版 schema，不需要。
        """
        self._registry = registry
        control = await psycopg.AsyncConnection.connect(self.uri, autocommit=True)
        self._control = control
        await control.execute(
            """
            create table if not exists running_turns (
                thread_id         text primary key,
                worker_id         text not null,
                started_at        timestamptz not null default now(),
                heartbeat_at      timestamptz not null default now(),
                stop_requested_at timestamptz,
                answer            text,
                stopped_at        timestamptz
            )
            """
        )
        self._listener = asyncio.create_task(self._listen_forever())
        logger.info("running_turns 就绪，worker_id={}", self.worker)

    async def aclose(self) -> None:
        """停 listener、收掉在跑的分发、关连接。"""
        if self._listener is not None:
            self._listener.cancel()
            try:
                await self._listener
            except asyncio.CancelledError:
                pass
            self._listener = None
        for task in list(self._dispatches):
            task.cancel()
        if self._control is not None:
            await self._control.close()
            self._control = None

    # ---------- 控制查询（表） ----------

    async def _execute(
        self, sql: str, params: tuple = ()
    ) -> list[tuple]:
        assert self._control is not None, "RunningTurns 还没 start"
        async with self._lock:
            cursor = await self._control.execute(sql, params)
            # 不带 RETURNING 的写语句没有结果集（description 为 None），fetch 会报错
            if cursor.description is None:
                return []
            return await cursor.fetchall()

    async def open_turn(self, thread_id: str) -> bool:
        """登记一轮。**PK 就是跨进程的互斥**：已经有在跑的轮次返回 ``False``（调用方转 409）。

        崩溃残留的行靠心跳回收（:data:`TURN_HEARTBEAT_TTL`），所以 ``False`` 也可能是
        “上一个 worker 挂了、心跳还没停满”——那种等几十秒就好。
        """
        rows = await self._execute(
            """
            insert into running_turns (thread_id, worker_id) values (%s, %s)
            on conflict (thread_id) do nothing
            returning thread_id
            """,
            (thread_id, self.worker),
        )
        return bool(rows)

    async def close_turn(self, thread_id: str) -> None:
        """轮次正常结束：删行。

        ``stop_requested_at is null`` 这个条件是必须的 —— 有人按了停止的话，那行要留着让
        请求方读走 ``answer``，不能在这删掉。
        """
        await self._execute(
            "delete from running_turns where thread_id = %s and stop_requested_at is null",
            (thread_id,),
        )

    async def request_stop(self, thread_id: str) -> StopRequest | None:
        """登记停止请求，并通知所有进程。

        返回 ``None`` = **没有在跑的轮次**（已经跑完 / 正等人批准 / 本来就没人跑）——
        调用方直接本地收尾就行，幂等，而且**不用等超时**。这一条是这张表最值钱的地方：
        没有它，请求方没法区分"别人在跑"和"根本没人在跑"。
        """
        rows = await self._execute(
            """
            update running_turns
               set stop_requested_at = coalesce(stop_requested_at, now())
             where thread_id = %s
            returning worker_id, answer, stopped_at
            """,
            (thread_id,),
        )
        if not rows:
            return None
        owner, answer, stopped_at = rows[0]
        await self._execute("select pg_notify(%s, %s)", (CHANNEL, thread_id))
        request = StopRequest(owner=owner, answer=answer if stopped_at else None)
        if answer and not stopped_at:  # 不该发生；真发生了说明有人直接写了 answer
            logger.warning("running_turns 有 answer 但没有 stopped_at：thread_id={}", thread_id)
        return request

    async def finish_stop(self, thread_id: str, answer: str) -> None:
        """owner 收尾完，把答案写回表。

        **不删行** —— 请求方要读走这个答案。删是请求方的事（:meth:`drop_stop`），
        或者等心跳停止后被自动收掉。
        """
        await self._execute(
            "update running_turns set answer = %s, stopped_at = now() where thread_id = %s",
            (answer, thread_id),
        )

    async def drop_stop(self, thread_id: str) -> None:
        """请求方读走结果后删行。重复调用无所谓。"""
        await self._execute("delete from running_turns where thread_id = %s", (thread_id,))

    async def wait_stopped(self, thread_id: str, timeout: float) -> str | None:
        """等 owner 收尾，返回它写回的答案；超时返回 ``None``。

        超时有两种可能，都只能由调用方兜底收尾：**worker 崩了**（没人认领那条通知），
        或者 **owner 卡住**（工具在不可取消的阻塞调用里）。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            rows = await self._execute(
                "select answer, stopped_at from running_turns where thread_id = %s",
                (thread_id,),
            )
            if not rows:  # owner 已经把行删了（正常结束那一轮），没什么可等的
                return None
            answer, stopped_at = rows[0]
            if stopped_at:
                return answer or ""
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(POLL_INTERVAL)

    # ---------- listener ----------

    async def _listen_forever(self) -> None:
        """一条长连接 + 自动重连。

        断了**不会静默失效**：``notifies()`` 会抛 ``OperationalError``，这里接住重连。
        重连成功后补扫一遍表 —— 断开期间发出的通知已经丢了（通知是即发即弃的）。
        """
        while True:
            try:
                conn = await psycopg.AsyncConnection.connect(self.uri, autocommit=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("chat_drain 连不上，稍后重试：{}", exc)
                await asyncio.sleep(SWEEP_INTERVAL)
                continue
            try:
                await conn.execute(f"listen {CHANNEL}")
                self._listener_conn = conn
                logger.info("chat_drain 已订阅")
                await self._replay_pending()
                while True:
                    async for note in conn.notifies(timeout=SWEEP_INTERVAL):
                        self._spawn_dispatch(str(note.payload))
                    await self._sweep()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("chat_drain 连接断了，重连中：{}", exc)
            finally:
                self._listener_conn = None
                await conn.close()

    def _spawn_dispatch(self, thread_id: str) -> None:
        """每条通知单独一个 task —— 一次慢的收尾不能堵住这个进程其它会话的停止。"""
        task = asyncio.create_task(self._dispatch(thread_id))
        self._dispatches.add(task)
        task.add_done_callback(self._dispatches.discard)

    async def _dispatch(self, thread_id: str) -> None:
        """收到通知：是我的轮次就交给 owner 收尾，别人的直接忽略。"""
        registry = self._registry
        turn = registry.get(thread_id) if registry is not None else None
        if turn is None:
            return
        try:
            await self._on_stop(thread_id, turn)
        except Exception:  # noqa: BLE001
            logger.exception("收尾失败：thread_id={}", thread_id)

    async def _replay_pending(self) -> None:
        """重连/启动兜底：把"已请求停止但还没收尾"的行补一遍。"""
        rows = await self._execute(
            """
            select thread_id from running_turns
             where stop_requested_at is not null and stopped_at is null
            """
        )
        for (thread_id,) in rows:
            self._spawn_dispatch(thread_id)

    async def _sweep(self) -> None:
        """两件事，都挂在 listener 的空闲周期上（不另起定时器）：

        1. **给自己在跑的轮次打心跳**（所以 worker 一挂，那行在 ``TURN_HEARTBEAT_TTL``
           后就会被当陈的收掉 —— 不用等一个很长的 TTL）；
        2. **收掉心跳停了的行**。
        """
        registry = self._registry
        mine = registry.names() if registry is not None else []
        if mine:
            await self._execute(
                "update running_turns set heartbeat_at = now() where worker_id = %s and thread_id = any(%s)",
                (self.worker, mine),
            )
        rows = await self._execute(
            """
            delete from running_turns
             where heartbeat_at < now() - %s
            returning thread_id, worker_id
            """,
            (TURN_HEARTBEAT_TTL,),
        )
        for thread_id, worker in rows:
            logger.warning(
                "收掉心跳停了 {} 的轮次登记（worker 多半挂了）：thread_id={} worker={}",
                TURN_HEARTBEAT_TTL,
                thread_id,
                worker,
            )