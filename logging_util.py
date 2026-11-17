"""结构化 JSON 日志：每行一条 JSON，携带 request_id，便于采集与检索。"""
from __future__ import annotations

import json
import sys
import threading
import time
import uuid

_seq = 0
_lock = threading.Lock()


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def log(level: str, event: str, request_id: str = "", **fields) -> None:
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "level": level,
        "event": event,
        "request_id": request_id,
    }
    for k, v in fields.items():
        if k in ("token", "agent_token", "code", "password", "authorization"):
            rec[k] = "***"
        else:
            rec[k] = v
    print(json.dumps(rec, ensure_ascii=False), file=sys.stdout, flush=True)


def info(event: str, request_id: str = "", **fields) -> None:
    log("info", event, request_id, **fields)


def warn(event: str, request_id: str = "", **fields) -> None:
    log("warn", event, request_id, **fields)


def error(event: str, request_id: str = "", **fields) -> None:
    log("error", event, request_id, **fields)
