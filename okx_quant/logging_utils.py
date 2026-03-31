from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from okx_quant.config import Settings


def configure_logging(settings: Settings) -> None:
    root = logging.getLogger()
    if getattr(configure_logging, "_configured", False):
        return

    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    log_path = Path(settings.log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    configure_logging._configured = True
