"""智能体依赖：从全局 AgentService 取 DeepAgent 实例。"""

from typing import Annotated

from fastapi import Depends

from agents import DeepAgent
from services.agent import agent_service

__all__ = ["AgentDep", "get_agent"]


def get_agent() -> DeepAgent:
    return agent_service.get()


AgentDep = Annotated[DeepAgent, Depends(get_agent)]
