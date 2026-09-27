"""Agent 依赖：从 ``app.state`` 取 lifespan 里启动好的 ``GeneralAgent``。"""

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from agents.agent import GeneralAgent

__all__ = ["AgentDep", "get_agent"]


def get_agent(request: Request) -> GeneralAgent:
    """路由里 ``agent: AgentDep`` 即可拿到运行中的 agent。

    起不来（模型没配、正在启动、已关停）时给 503 —— 这是服务端环境问题，
    不是请求本身的问题，别混进 4xx 里让调用方以为要改请求。
    """
    agent: GeneralAgent | None = getattr(request.app.state, "agent", None)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent 未就绪（未配置模型或服务正在启动）",
        )
    return agent


AgentDep = Annotated[GeneralAgent, Depends(get_agent)]
