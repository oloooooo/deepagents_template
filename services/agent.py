"""智能体服务：应用级单例，负责 DeepAgent 的启动与关闭。

DeepAgent 内部持有 PostgreSQL 连接池（checkpointer + store），所以由应用
lifespan 统一 start/stop，请求之间复用同一份连接与编译好的图。
"""

from agents import DeepAgent


class AgentService:
    """持有全局 DeepAgent 实例。"""

    def __init__(self) -> None:
        self._agent: DeepAgent | None = None

    async def start(self) -> DeepAgent:
        """启动智能体（幂等）：建表 + 编译图 + 建连接池。"""
        if self._agent is None:
            self._agent = await DeepAgent().aenter()
        return self._agent

    async def stop(self) -> None:
        """关闭智能体（幂等）：释放连接池。"""
        agent, self._agent = self._agent, None
        if agent is not None:
            await agent.aexit()

    def get(self) -> DeepAgent:
        """取已启动的智能体实例。"""
        if self._agent is None:
            raise RuntimeError("DeepAgent 尚未启动（应在应用 lifespan 里调用 agent_service.start()）")
        return self._agent


agent_service = AgentService()

__all__ = ["AgentService", "agent_service"]
