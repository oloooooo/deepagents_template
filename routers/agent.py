"""智能体路由。

全部接口都要求用户 access token（`CurrentUser`），且 thread_id 会按用户 id 做前缀隔离，
因此拿不到别人的会话。命名空间同理：`/memories/` 的文件按用户分库存。

- `POST /agent/chat`：跑一轮对话，返回完整回答
- `POST /agent/stream`：同上，改用 SSE 逐 token 推送
- `GET  /agent/state/{thread_id}`：读短时记忆（检查点里的消息与回答）
- `GET  /agent/memories`：读长期记忆（store 里保存的文件列表）
"""

import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Path
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from dependencies import AgentDep
from dependencies.auth import CurrentUser
from logger import logger

router = APIRouter(prefix="/agent", tags=["agent"])


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=8000, description="用户输入")
    thread_id: str = Field(
        default="default",
        min_length=1,
        max_length=64,
        description="会话 id：同一个 id 续聊同一段上下文（短时记忆）",
    )


class ChatResponse(BaseModel):
    thread_id: str
    answer: str


class MemoryResponse(BaseModel):
    thread_id: str
    user_id: str
    messages: int = Field(description="检查点里累计的消息条数")
    answer: str = Field(description="最后一条 AI 回答")
    files: list[str] = Field(description="该会话 state 里存在的临时文件")


@router.post("/chat", response_model=ChatResponse, summary="对话（一次性返回）")
async def chat(
    payload: ChatRequest, current_user: CurrentUser, agent: AgentDep
) -> ChatResponse:
    answer = await agent.ainvoke(
        payload.message, thread_id=payload.thread_id, user_id=current_user.id
    )
    return ChatResponse(thread_id=payload.thread_id, answer=answer)


@router.post("/stream", summary="对话（SSE 流式返回）")
async def stream(
    payload: ChatRequest, current_user: CurrentUser, agent: AgentDep
) -> StreamingResponse:
    thread_id, user_id = payload.thread_id, current_user.id

    async def event_source() -> AsyncIterator[str]:
        try:
            async for event in agent.astream(payload.message, thread_id=thread_id, user_id=user_id):
                data = {"kind": event.kind, "text": event.text}
                if event.kind == "done":
                    data["thread_id"] = event.data.get("thread_id", thread_id)
                    data["messages"] = event.data.get("messages")
                yield f"event: {event.kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        except Exception as exc:  # 响应头已发出，只能把错误当事件推给客户端
            logger.exception("SSE 对话失败: {}", exc)
            yield f"event: error\ndata: {json.dumps({'detail': str(exc)}, ensure_ascii=False)}\n\n"
        yield "event: end\ndata: [DONE]\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/state/{thread_id}", response_model=MemoryResponse, summary="读短时记忆")
async def get_state(
    current_user: CurrentUser,
    agent: AgentDep,
    thread_id: Annotated[str, Path(min_length=1, max_length=64)],
) -> MemoryResponse:
    state = await agent.aget_state(thread_id, current_user.id)
    return MemoryResponse(**state)


@router.get("/memories", response_model=list[str], summary="读长期记忆文件列表")
async def list_memories(current_user: CurrentUser, agent: AgentDep) -> list[str]:
    return await agent.alist_memories(current_user.id)
