"""路由的请求 / 响应模型（pydantic），按资源分文件。"""

from .auth import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UserOut,
)
from .chat import (
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
from .memory import (
    MemoryListOut,
    MemoryOut,
    MemoryPath,
    MemoryStored,
    MemoryUploadItem,
    MemoryUploadOut,
    MemoryWrite,
)

__all__ = [
    "ChatApprove",
    "ChatDeleteFiles",
    "ChatDeleteMessages",
    "ChatHistoryOut",
    "ChatMessageOut",
    "ChatRunOut",
    "ChatSend",
    "ChatStateOut",
    "ChatStop",
    "ChatStopOut",
    "ChatThreadOut",
    "ChatThreadsOut",
    "LoginRequest",
    "MemoryListOut",
    "MemoryOut",
    "MemoryPath",
    "MemoryStored",
    "MemoryUploadItem",
    "MemoryUploadOut",
    "MemoryWrite",
    "RefreshRequest",
    "RegisterRequest",
    "TokenPair",
    "UserOut",
]
