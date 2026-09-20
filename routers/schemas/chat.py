"""聊天路由的请求 / 响应模型。

与记忆一样：**没有 user_id 字段**且 ``extra="forbid"``，归属只由登录态决定。
``thread_id`` 是前端拿到的短 id（不带用户前缀），服务端会拼上 ``{user_id}:`` 再落库。
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from models import DEFAULT_WORKSPACE

__all__ = [
    "ChatApprove",
    "ChatDeleteMessages",
    "ChatDeleteFiles",
    "ChatHistoryOut",
    "ChatMessageOut",
    "ChatRunOut",
    "ChatSend",
    "ChatStateOut",
    "ChatStop",
    "ChatStopOut",
    "ChatThreadOut",
    "ChatThreadsOut",
]

# 短 thread_id：只允许 url 安全字符，且不含 ":"（":" 是用户前缀的分隔符）
THREAD_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"


class ChatSend(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(
        DEFAULT_WORKSPACE,
        min_length=1,
        max_length=32,
        description="业务空间 id；不传就落虚拟的 default 空间（日常聊天）",
    )
    message: str = Field(min_length=1, max_length=10_000, description="用户这一轮说的话")
    thread_id: str | None = Field(
        default=None,
        pattern=THREAD_ID_PATTERN,
        description="续聊时带上上次返回的 thread_id；不传就新开一个会话",
    )


class ChatApprove(BaseModel):
    """人工批准：``decisions`` 原样透传给 langgraph 的 ``resume``。"""

    model_config = ConfigDict(extra="forbid")

    thread_id: str = Field(pattern=THREAD_ID_PATTERN)
    decisions: list[dict[str, Any]] = Field(
        min_length=1,
        description='形如 [{"type": "approve"}]，也支持 edit / reject / respond',
    )


class ChatRunOut(BaseModel):
    thread_id: str
    answer: str = Field(description="最终回答；被拦下时为空串")
    interrupt: dict[str, Any] | None = Field(
        default=None, description="非空表示这轮在等人批准，原样返回给前端展示"
    )


class ChatStop(BaseModel):
    """停止这一轮（用户按暂停键）。

    与 ``ChatApprove`` 的区别：批准是接着跑（中断，agent 的动作），
    停止是到此为止（不可续跑，用户的动作）。见 ``CONTEXT.md``。
    """

    model_config = ConfigDict(extra="forbid")

    thread_id: str = Field(pattern=THREAD_ID_PATTERN)


class ChatStopOut(BaseModel):
    thread_id: str
    answer: str = Field(
        description="这一轮最终留在历史里的文本：已流出的部分文本，或占位文案；"
        "已经跑完的轮次返回原来的回答"
    )


class ChatStateOut(BaseModel):
    thread_id: str
    workspace_id: str | None
    messages: int = Field(description="该会话短期记忆里的消息条数")
    answer: str = Field(description="最后一条有内容的回答")
    files: list[str] = Field(description="会话内临时文件（StateBackend）路径")


class ChatThreadOut(BaseModel):
    thread_id: str
    workspace_id: str | None
    updated_at: str | None = Field(default=None, description="检查点时间戳（ISO）")


class ChatThreadsOut(BaseModel):
    threads: list[ChatThreadOut]


class ChatMessageOut(BaseModel):
    """短期记忆里的一条消息（``id`` 就是删单条时要传的值）。"""

    id: str
    role: str = Field(description="human / ai / tool / system")
    content: str


class ChatHistoryOut(BaseModel):
    thread_id: str
    messages: list[ChatMessageOut] = Field(description="最旧→最新")


class ChatDeleteMessages(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str = Field(pattern=THREAD_ID_PATTERN)
    message_ids: list[str] = Field(
        min_length=1, description="要删的消息 id（来自 /chat/history），删完还能继续聊"
    )


class ChatDeleteFiles(BaseModel):
    """删会话内临时文件（StateBackend 的 ``files``）。"""

    model_config = ConfigDict(extra="forbid")

    thread_id: str = Field(pattern=THREAD_ID_PATTERN)
    paths: list[str] = Field(min_length=1, description="如 /tmp.txt")
