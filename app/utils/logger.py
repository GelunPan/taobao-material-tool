"""通用日志模块：按天分割日志文件，同时输出到控制台和文件。

日志目录：data/logs/（已在 .gitignore 中，不会上传 git）
用法：
    from app.utils.logger import get_logger
    logger = get_logger("taobao")
    logger.info("登录成功")
    logger.error("抓取失败: %s", e)
"""
import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from ..config import DATA_DIR

# 日志目录
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# 日志格式
LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 已初始化的 logger 缓存
_loggers: dict[str, logging.Logger] = {}


def get_logger(name: str = "app", level: int = logging.DEBUG) -> logging.Logger:
    """获取配置好的 logger，同一个 name 只初始化一次。

    参数:
        name: logger 名称（建议用模块名，如 "taobao"、"taobao.login"）
        level: 日志级别，默认 DEBUG

    返回:
        配置好的 Logger 对象，同时输出到控制台和按天分割的文件
    """
    if name in _loggers:
        return _loggers[name]

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False  # 避免重复输出到 root logger

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    # 控制台输出（INFO 及以上）
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 文件输出（DEBUG 及以上，按天分割，保留 30 天）
    log_file = LOG_DIR / f"{name}.log"
    file_handler = TimedRotatingFileHandler(
        filename=str(log_file),
        when="midnight",       # 每天午夜分割
        interval=1,
        backupCount=30,        # 保留 30 天
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    _loggers[name] = logger
    return logger
