"""``/kb/`` 对 agent 的静态权限：一条 deny 挡掉所有写（docs/adr/0003）。"""

from deepagents.middleware.filesystem import FilesystemPermission

from agents.kb.storage import KB_ROUTE

__all__ = ["KB_PERMISSIONS"]

KB_PERMISSIONS = [
    FilesystemPermission(
        operations=["write"],
        # 规则**按顺序第一条命中即生效**；裸 "/kb" 必须单列——
        # 没有尾斜杠的 "/kb" 匹配不上 "/kb/**"，实测能绕过去写到挂载根（/memories/ 踩过）
        paths=[f"{KB_ROUTE}**", KB_ROUTE.rstrip("/")],
        mode="deny",
    ),
]
"""知识库对**所有人**（含 super 的 agent 会话）只读；super 的写走 REST（/kb/files/write）。

这是编译期定死的规则，区分不了用户——但用户本来就一律只读，一条静态 deny 就是完整表达
（对比 ``MEMORY_PERMISSIONS`` 的 interrupt：那边有「agent 自己的地盘」，知识库没有）。
"""
