"""请求体里「名字」和「store 内路径」的共用校验。

记忆路径（``/memories/``）和公共空间路径（``/public/``）落在**同一个** langgraph store 里，
所以规则必须一致：挡掉 ``..`` / ``~`` / 空路径，别把脏路径带进 store。名字同理，
公共空间名会变成 agent 挂载路径 ``/public/{name}/`` 的一段，业务空间名会变成
``/memories/{name}/`` 的一段，字符集必须一致（否则名字里的斜杠会把路径拆歪）。
"""

from pathlib import PurePosixPath

__all__ = ["NAME_PATTERN", "safe_store_path"]

NAME_PATTERN = r"^[A-Za-z0-9_.\-]+$"
"""空间名的字符集：只允许字母数字和 ``_ . -``，保证能安全地当路径的一段。"""


def safe_store_path(value: str, *, label: str = "路径") -> str:
    """校验一个要写进 store 的相对路径，不合法直接 ``ValueError``（pydantic 会转成 422）。"""
    if not value.strip().strip("/"):
        raise ValueError(f"{label}不能为空")
    if ".." in PurePosixPath(value.replace("\\", "/")).parts or value.startswith("~"):
        raise ValueError(f"{label}不能包含 .. 或 ~")
    return value
