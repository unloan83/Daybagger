from __future__ import annotations
import json, logging
from datetime import datetime, timezone

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {'ts_utc': datetime.now(timezone.utc).isoformat(), 'level': record.levelname, 'logger': record.name, 'message': record.getMessage()}
        extra = getattr(record, 'event_data', None)
        if isinstance(extra, dict): payload['data'] = extra
        return json.dumps(payload, sort_keys=True, default=str)

def configure_logging(level: str='INFO') -> None:
    root = logging.getLogger(); root.handlers.clear()
    h = logging.StreamHandler(); h.setFormatter(JsonFormatter()); root.addHandler(h)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
