import json
import logging
from typing import Any

from app.config import settings


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        context = getattr(record, "context", None)
        if isinstance(context, dict):
            payload.update(context)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        from app.services.diagnostics import redact, scope, RUN_ID
        return json.dumps(redact({"run_id": RUN_ID, **scope.get(), **payload}), ensure_ascii=False, default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    from app.services.diagnostics import install
    install(root)


def event(logger: logging.Logger, message: str, **context: Any) -> None:
    logger.info(message, extra={"context": context})
