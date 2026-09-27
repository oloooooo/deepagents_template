"""聊天路由：/chat/*。

一个操作一条独立路径，动作词进路径；``thread_id`` 走 body/路径参数，语义都一样：

======================  ======  ================================================
路径                     方法     说明
======================  ======  ================================================
/chat/send              POST    跑一轮（阻塞），返回 answer，被拦下时返回 interrupt
/chat/stream            POST    跑一轮（SSE：token / tool_call / interrupt / done）
/chat/approve           POST    人工批准（或拒绝）后接着跑，decisions 原样透传
/chat/stop              POST    停止这一轮（用户按暂停），返回最终留在历史里的文本
/chat/mine              GET     我的会话列表（来自 checkpoint metadata）
/chat/state/{thread}    GET     某个会话的短期记忆概况
/chat/history/{thread}  GET     某个会话的消息列表（短期记忆读取）
/chat/messages/delete   POST    删几条消息（短期记忆编辑）
/chat/files/delete      POST    删会话内临时文件（StateBackend 的 files）
/chat/delete/{thread}   DELETE  删整条会话（只删短期记忆，不动 /memories/）
======================  ======  ================================================

鉴权：``user_id`` 只来自登录态；``thread_id`` 先过归属校验（非本人 / 不存在一律 404）。

SSE 事件格式（每个事件都是 ``event: <kind>`` + 一行 JSON）：

- ``token``：``{"text": "增量文本"}``
- ``tool_call``：``{"text": "工具名", "data": {"id": ...}}``
- ``interrupt``：``{"text": "", "data": {"action_requests": [...], "review_configs": [...]}}``
- ``done``：``{"text": "完整回答"}``（正常跑完一定以它收尾）

被 ``/chat/stop`` 停掉时流是**直接断的**（任务被取消，没有机会再 yield）：按停止的那一端
本来就知道自己停了，结果从 ``/chat/stop`` 的响应体里取；其它标签页重新拉 ``/chat/state``
或 ``/chat/history`` 就能看到收尾后的历史。

新建的 thread_id 通过响应头 ``X-Thread-Id`` 返回（SSE 场景没有 JSON body 可放）。

同一会话同时只允许一轮在跑：重复提交返回 409（停止是异步的，``await`` 完 ``/chat/stop``
再发新消息就不会撞上）。
"""

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Response, status
from fastapi.responses import StreamingResponse

from agents.agent import AgentEvent
from dependencies.agent import AgentDep
from dependencies.auth import CurrentUser
from dependencies.kb import KbCellsDep
from routers.schemas import (
    ChatApprove,
    ChatDeleteFiles,
    ChatDeleteMessages,
    ChatHistoryOut,
    ChatMessageOut,
    ChatRunOut,
    ChatSend,
    ChatStateOut,
    ChatStop,
    ChatStopOut,
    ChatThreadOut,
    ChatThreadsOut,
)
from services import ChatService

__all__ = ["router"]

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/send", response_model=ChatRunOut, summary="跑一轮对话（阻塞）")
async def send_message(
    payload: ChatSend,
    current_user: CurrentUser,
    kb_cells: KbCellsDep,
    agent: AgentDep,
):
    thread_id, run = await ChatService(agent).send(
        current_user,
        message=payload.message,
        thread_id=payload.thread_id,
        kb_cells=kb_cells,
    )
    return ChatRunOut(
        thread_id=thread_id, answer=run.answer, interrupt=run.interrupt
    )


@router.post("/stream", summary="跑一轮对话（SSE 流式）")
async def stream_message(
    payload: ChatSend,
    current_user: CurrentUser,
    kb_cells: KbCellsDep,
    agent: AgentDep,
) -> StreamingResponse:
    thread_id, events = await ChatService(agent).stream(
        current_user,
        message=payload.message,
        thread_id=payload.thread_id,
        kb_cells=kb_cells,
    )
    return StreamingResponse(
        _sse(events),
        media_type="text/event-stream",
        headers={"X-Thread-Id": thread_id, "Cache-Control": "no-cache"},
    )


@router.post(
    "/approve",
    response_model=ChatRunOut,
    summary="人工批准 / 拒绝后接着跑",
)
async def approve_message(
    payload: ChatApprove,
    current_user: CurrentUser,
    kb_cells: KbCellsDep,
    agent: AgentDep,
):
    run = await ChatService(agent).approve(
        current_user,
        thread_id=payload.thread_id,
        decisions=payload.decisions,
        kb_cells=kb_cells,
    )
    return ChatRunOut(
        thread_id=payload.thread_id, answer=run.answer, interrupt=run.interrupt
    )


@router.post(
    "/stop",
    response_model=ChatStopOut,
    summary="停止这一轮（用户按暂停）",
)
async def stop_turn(
    payload: ChatStop,
    current_user: CurrentUser,
    agent: AgentDep,
):
    """取消在跑的那一轮并把短期记忆收尾；没有在跑的轮次也返回 200（幂等）。

    三种情况都能调：正在跑（取消）、正等人批准（丢弃待批准请求）、已经跑完（什么都不做）。
    """
    answer = await ChatService(agent).stop(
        current_user, thread_id=payload.thread_id
    )
    return ChatStopOut(thread_id=payload.thread_id, answer=answer)


@router.get("/mine", response_model=ChatThreadsOut, summary="我的会话列表")
async def list_my_threads(
    current_user: CurrentUser,
    agent: AgentDep,
):
    threads = await ChatService(agent).list_threads(current_user)
    return ChatThreadsOut(threads=[ChatThreadOut(**thread) for thread in threads])


@router.get(
    "/state/{thread_id}",
    response_model=ChatStateOut,
    summary="某个会话的短期记忆概况",
)
async def get_thread_state(
    thread_id: str,
    current_user: CurrentUser,
    agent: AgentDep,
):
    service = ChatService(agent)
    await service.own(current_user, thread_id)
    state = await service.memory.aget_state(thread_id, current_user.id)
    return ChatStateOut(
        thread_id=thread_id,
        messages=state["messages"],
        answer=state["answer"],
        files=state["files"],
    )


@router.get(
    "/history/{thread_id}",
    response_model=ChatHistoryOut,
    summary="某个会话的消息列表（短期记忆读取）",
)
async def get_thread_history(
    thread_id: str,
    current_user: CurrentUser,
    agent: AgentDep,
):
    messages = await ChatService(agent).history(current_user, thread_id)
    return ChatHistoryOut(
        thread_id=thread_id, messages=[ChatMessageOut(**message) for message in messages]
    )


@router.post(
    "/messages/delete",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="删几条消息（短期记忆编辑）",
)
async def delete_thread_messages(
    payload: ChatDeleteMessages,
    current_user: CurrentUser,
    agent: AgentDep,
) -> None:
    await ChatService(agent).delete_messages(
        current_user, thread_id=payload.thread_id, message_ids=payload.message_ids
    )


@router.post(
    "/files/delete",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="删会话内临时文件（短期记忆编辑）",
)
async def delete_thread_files(
    payload: ChatDeleteFiles,
    current_user: CurrentUser,
    agent: AgentDep,
) -> None:
    await ChatService(agent).delete_files(
        current_user, thread_id=payload.thread_id, paths=payload.paths
    )


@router.delete(
    "/delete/{thread_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="删整条会话（短期记忆，不影响长期记忆）",
)
async def delete_thread(
    thread_id: str,
    current_user: CurrentUser,
    agent: AgentDep,
) -> None:
    await ChatService(agent).delete_thread(current_user, thread_id)


def _sse(events: AsyncIterator[AgentEvent]) -> AsyncIterator[str]:
    """把 ``AgentEvent`` 转成 SSE 文本帧（``event:`` + 一行 JSON）。

    统一成 ``{"text": ..., "data": ...}``，``data`` 只在有内容时才带。
    新建的 thread_id 通过响应头 ``X-Thread-Id`` 返回。
    """

    async def frames() -> AsyncIterator[str]:
        async for event in events:
            payload: dict = {"text": event.text}
            if event.data:
                payload["data"] = event.data
            yield f"event: {event.kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return frames()
