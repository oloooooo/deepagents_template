"""FastAPI 应用入口。

本地启动::

    uv run python main.py                                      # 开发（reload，日志统一走 loguru）
    uv run alembic upgrade head                                # 首次使用前初始化 users 表
    uv run uvicorn main:app --loop asyncio:SelectorEventLoop   # Windows 非 reload 时必须带 --loop
"""

import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI

from config import app_config
from dependencies import engine
from logger import logger
from routers import auth, workspace

# Windows 默认的 ProactorEventLoop 跑不了 psycopg 异步驱动，
# 而 uvicorn 非 reload 模式恰好会选 Proactor，所以显式指定 SelectorEventLoop。
LOOP = "asyncio:SelectorEventLoop" if sys.platform == "win32" else "auto"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = app_config.postgresql.user
    logger.info("应用启动，业务库 {}@{}:{}/{}", db.user, db.host, db.port, db.db_name)
    yield
    await engine.dispose()
    logger.info("应用退出，数据库连接池已释放")


app = FastAPI(title="DeepAgents Template", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(workspace.router)


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
