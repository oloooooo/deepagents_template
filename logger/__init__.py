"""loguru 日志配置。

对外暴露 ``logger``（已配置好的 loguru logger）与 ``setup_logger()``（换配置后重新初始化）。
标准库 logging（uvicorn / sqlalchemy / alembic 等）经 InterceptHandler 统一转发到 loguru，
保证控制台与文件日志格式一致。
"""

import logging
import sys

from loguru import logger as _logger

from config import app_config

__all__ = ["InterceptHandler", "logger", "setup_logger"]

_CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)
_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}"
)

_INTERNAL_FRAMES = frozenset({logging.__file__, __file__})

# 需要接管的标准库 logger（uvicorn 启动时会自己配置 handler，故逐个替换）
_INTERCEPTED_LOGGERS = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "fastapi",
    "sqlalchemy",
    "alembic",
)


class InterceptHandler(logging.Handler):
    """把标准库 logging 的记录转发给 loguru。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = _logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # 从 emit 帧往上找第一个不属于 logging / 本文件的帧（即真正打日志的代码），
        # 跳过的帧数就是 loguru 需要的 depth，这样 name/function/line 才指向调用处
        frame, depth = logging.currentframe(), 0
        while frame is not None and frame.f_code.co_filename in _INTERNAL_FRAMES:
            frame = frame.f_back
            depth += 1

        _logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def setup_logger():
    """按 app_config.logger 重新初始化 loguru 的控制台与文件 sink。"""
    cfg = app_config.logger
    log_dir = cfg.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    # 先移除 loguru 默认 sink，重复调用不会出现重复日志
    _logger.remove()
    _logger.add(
        sys.stderr,
        level=cfg.level,
        format=_CONSOLE_FORMAT,
        colorize=True,
        backtrace=cfg.backtrace,
        diagnose=cfg.diagnose,
    )
    _logger.add(
        log_dir / "app_{time:YYYY-MM-DD}.log",
        level=cfg.level,
        format=_FILE_FORMAT,
        rotation=cfg.rotation,
        retention=cfg.retention,
        encoding="utf-8",
        backtrace=cfg.backtrace,
        diagnose=cfg.diagnose,
    )

    # 标准库日志全部走 loguru
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    for name in _INTERCEPTED_LOGGERS:
        std_logger = logging.getLogger(name)
        std_logger.handlers = [InterceptHandler()]
        std_logger.propagate = False

    return _logger


logger = setup_logger()
