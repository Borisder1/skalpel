import builtins
import logging
import os
import re
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler

_ORIGINAL_PRINT = builtins.print
_CONFIGURED = False


def redact_secrets(text: object) -> str:
    """Mask API secrets before logs are written to Render stdout/files."""
    value = str(text)
    value = re.sub(r"/bot[^/]+/", "/bot***/", value)
    value = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot***", value)
    value = re.sub(r"\b\d{8,12}:[A-Za-z0-9_-]{20,}\b", "***TELEGRAM_TOKEN***", value)
    value = re.sub(r"rnd_[A-Za-z0-9_-]+", "rnd_***", value)
    return value


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact_secrets(super().format(record))


def setup_file_logging(app_name: str = "bot") -> str:
    """Configure root logging to Render stdout and rotating local log file.

    Also routes plain print(...) calls through logging so existing bot modules
    are persisted to logs/bot_YYYYMMDD.log without a risky full rewrite.
    """
    global _CONFIGURED

    repo_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(repo_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_filename = os.path.join(log_dir, f"{app_name}_{datetime.now().strftime('%Y%m%d')}.log")

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    if not _CONFIGURED:
        formatter = RedactingFormatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        file_handler = RotatingFileHandler(
            log_filename,
            maxBytes=5 * 1024 * 1024,
            backupCount=10,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.INFO)

        console_handler = logging.StreamHandler(sys.__stdout__)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(logging.INFO)

        logger.handlers.clear()
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)

        def logging_print(*args, sep=" ", end="\n", file=None, flush=False):
            message = sep.join(str(arg) for arg in args)
            if end and end != "\n":
                message = f"{message}{end}"
            level = logging.ERROR if file is sys.stderr else logging.INFO
            print_logger = logging.getLogger("print")
            for line in redact_secrets(message).splitlines() or [""]:
                print_logger.log(level, line)
            for handler in logging.getLogger().handlers:
                if flush:
                    handler.flush()

        builtins.print = logging_print
        _CONFIGURED = True

    logger.info("=" * 60)
    logger.info("БОТ ЗАПУЩЕНО")
    logger.info("Лог-файл: %s", log_filename)
    logger.info("Час запуску: %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("=" * 60)
    return log_filename
