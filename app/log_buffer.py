# log_buffer.py - 内存日志缓冲，供 Web 控制台实时展示
import asyncio
import logging
import re
import threading
from collections import deque
from datetime import datetime
from typing import Deque, Dict, List, Optional, Set

# 推送到前端前脱敏，避免 Cookie / Key 出现在浏览器日志流里
_SECRET_PATTERNS = [
    re.compile(r"(SESSDATA|sessdata|bili_jct|buvid3|ac_time_value)\s*[=:]\s*([^\s,;\"']+)", re.I),
    re.compile(r"(api[_-]?key|authorization|bearer|web_password)\s*[=:]\s*([^\s,;\"']+)", re.I),
    re.compile(r"\b(sk-[A-Za-z0-9]{8,})\b"),
    re.compile(r"\b(eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b"),
]


def sanitize_log_message(message: str) -> str:
    text = str(message or "")
    for pat in _SECRET_PATTERNS:
        if pat.groups >= 2:
            text = pat.sub(lambda m: f"{m.group(1)}=***", text)
        else:
            text = pat.sub("***", text)
    return text


class LogBuffer:
    """线程安全的环形日志缓冲，支持异步订阅推送。"""

    def __init__(self, maxlen: int = 2000):
        self._records: Deque[Dict] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = 0
        self._subscribers: Set[asyncio.Queue] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def append(self, level: str, message: str, created: float = None):
        with self._lock:
            self._seq += 1
            item = {
                "id": self._seq,
                "level": level,
                "message": sanitize_log_message(message),
                "time": datetime.fromtimestamp(created or datetime.now().timestamp()).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            }
            self._records.append(item)
            snapshot = dict(item)

        self._notify(snapshot)

    def _notify(self, item: Dict):
        loop = self._loop
        if not loop or not self._subscribers:
            return

        def _push():
            dead = []
            for q in list(self._subscribers):
                try:
                    q.put_nowait(item)
                except asyncio.QueueFull:
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        q.put_nowait(item)
                    except asyncio.QueueFull:
                        dead.append(q)
                except Exception:
                    dead.append(q)
            for q in dead:
                self._subscribers.discard(q)

        try:
            loop.call_soon_threadsafe(_push)
        except RuntimeError:
            pass

    def recent(self, limit: int = 200, after_id: int = 0) -> List[Dict]:
        with self._lock:
            items = [r for r in self._records if r["id"] > after_id]
        if limit > 0:
            items = items[-limit:]
        return items

    def subscribe(self, maxsize: int = 500) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subscribers.discard(q)


class MemoryLogHandler(logging.Handler):
    """把 logging 记录写入 LogBuffer。"""

    def __init__(self, buffer: LogBuffer):
        super().__init__()
        self.buffer = buffer

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            # 去掉时间前缀，前端自己显示 time 字段
            # 格式: 2026-01-01 12:00:00 - LEVEL - message
            parts = msg.split(" - ", 2)
            if len(parts) == 3:
                msg = parts[2]
            self.buffer.append(record.levelname, msg, record.created)
        except Exception:
            self.handleError(record)


log_buffer = LogBuffer(maxlen=3000)
