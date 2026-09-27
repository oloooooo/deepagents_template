"""知识库（agent 侧）：挂载 ``/kb/``、静态只读权限、存储后端工厂。"""

from agents.kb.mount import KbMountBackend, kb_mount
from agents.kb.permissions import KB_PERMISSIONS
from agents.kb.storage import KB_ROUTE

__all__ = ["KB_PERMISSIONS", "KB_ROUTE", "KbMountBackend", "kb_mount"]
