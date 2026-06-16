"""
core/logger.py
Colored console + file logging for the visa bot.
"""
import logging
import colorlog
from pathlib import Path

LOG_FILE = Path(__file__).parent.parent / "logs" / "visa_bot.log"
LOG_FILE.parent.mkdir(exist_ok=True)


def get_logger(name: str = "visa_bot") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # Console handler (colored)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s [%(levelname)s]%(reset)s %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            "DEBUG":    "cyan",
            "INFO":     "green",
            "WARNING":  "yellow",
            "ERROR":    "red",
            "CRITICAL": "bold_red",
        }
    ))

    # File handler
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))

    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger


log = get_logger()
