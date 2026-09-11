"""Unchanged from v2 — this module was already solid (leveled, timestamped,

optional JSON output). Ported as-is into the new package layout."""

from __future__ import annotations



import json

import logging

import sys



from .config import settings





class _JsonFormatter(logging.Formatter):

    def format(self, record: logging.LogRecord) -> str:

        payload = {

            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),

            "level": record.levelname,

            "logger": record.name,

            "message": record.getMessage(),

        }

        if record.exc_info:

            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)





def get_logger(name: str) -> logging.Logger:

    logger = logging.getLogger(name)

    if logger.handlers:

        return logger

    handler = logging.StreamHandler(sys.stdout)

    if settings.log_json:

        handler.setFormatter(_JsonFormatter())

    else:

        handler.setFormatter(logging.Formatter(

            "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S",

        ))

    logger.addHandler(handler)

    logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))

    logger.propagate = False

    return logger