"""FastAPI 应用入口。

本地启动::

    uv run python main.py                                      # 开发（reload，日志统一走 loguru）
    uv run alembic upgrade head                                # 首次使用前初始化 users 表
    uv run uvicorn main:app --loop asyncio:SelectorEventLoop   # Windows 非 reload 时必须带 --loop
"""

import sys
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from agents.agent import GeneralAgent
from agents.config import model_cfg
from config import app_config
from dependencies import engine
from logger import logger
from routers import auth, chat, memory, workspace

# Windows 默认的 ProactorEventLoop 跑不了 psycopg 异步驱动，
# 而 uvicorn 非 reload 模式恰好会选 Proactor，所以显式指定 SelectorEventLoop。
LOOP = "asyncio:SelectorEventLoop" if sys.platform == "win32" else "auto"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = app_config.postgresql.user
    logger.info("应用启动，业务库 {}@{}:{}/{}", db.user, db.host, db.port, db.db_name)
    # 与业务库无关：auth / workspace 路由不依赖 agent，所以没配模型也照常起服务，
    # 只有 agent 端点会 503（见 dependencies/agent.py）。配了模型但连不上库则直接启动失败。
    async with AsyncExitStack() as stack:
        if model_cfg.ready:
            app.state.agent = await stack.enter_async_context(GeneralAgent())
        else:
            logger.warning(
                "未读到模型配置（OPEN_MODEL / OPEN_BASE_URL / OPEN_API_KEY），agent 端点将返回 503"
            )
        yield
        app.state.agent = None
    await engine.dispose()
    logger.info("应用退出，数据库连接池已释放")


app = FastAPI(title="DeepAgents Template", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(workspace.router)
app.include_router(memory.router)
app.include_router(chat.router)


@app.get("/health", tags=["system"], summary="健康检查")
async def health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    # log_config=None: 不用 uvicorn 自己的日志配置，日志统一交给 loguru
    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        loop=LOOP,
        log_config=None,
    )
