# auth.py - Web 控制台访问控制
from __future__ import annotations

import hmac
import secrets
import time
from typing import Dict, Optional, Tuple

from ..config import ConfigManager


# token -> 过期时间戳
_sessions: Dict[str, float] = {}
_login_fail: Dict[str, Tuple[int, float]] = {}  # ip -> (次数, 窗口起始)

SESSION_TTL_SEC = 24 * 3600  # 24 小时，降低令牌长期暴露风险
MAX_FAILS = 5
FAIL_WINDOW_SEC = 600


def _app_cfg() -> dict:
    try:
        return ConfigManager().get_app_config() or {}
    except Exception:
        return {}


def get_web_password() -> str:
    pwd = _app_cfg().get("web_password")
    return str(pwd).strip() if pwd is not None else ""


def get_web_username() -> str:
    name = _app_cfg().get("web_username")
    text = str(name).strip() if name else "admin"
    return text or "admin"


def is_auth_enabled() -> bool:
    """配置了密码则启用鉴权。"""
    return bool(get_web_password())


def is_loopback_host(host: str) -> bool:
    h = (host or "").strip().lower()
    return h in ("127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1")


def require_password_for_host(host: str) -> bool:
    """非本机监听时必须配置密码。"""
    return not is_loopback_host(host)


def _purge_sessions():
    now = time.time()
    expired = [t for t, exp in _sessions.items() if exp <= now]
    for t in expired:
        _sessions.pop(t, None)


def create_session() -> str:
    _purge_sessions()
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_TTL_SEC
    return token


def revoke_session(token: Optional[str]):
    if token:
        _sessions.pop(token, None)


def verify_session(token: Optional[str]) -> bool:
    if not token:
        return False
    _purge_sessions()
    exp = _sessions.get(token)
    if not exp:
        return False
    if exp <= time.time():
        _sessions.pop(token, None)
        return False
    # 滑动续期
    _sessions[token] = time.time() + SESSION_TTL_SEC
    return True


def check_password(password: str) -> bool:
    expected = get_web_password()
    if not expected:
        return False
    given = str(password or "")
    # 长度不同时 pad 后再比较，避免异常并保持常量时间比较习惯
    if len(given) != len(expected):
        # 仍做一次 compare，防止明显的时序旁路被滥用为探测
        hmac.compare_digest(given.ljust(len(expected), "\0")[: len(expected)], expected)
        return False
    return hmac.compare_digest(given, expected)


def check_login_rate(ip: str) -> bool:
    """返回 True 表示允许继续尝试。"""
    now = time.time()
    count, start = _login_fail.get(ip, (0, now))
    if now - start > FAIL_WINDOW_SEC:
        _login_fail[ip] = (0, now)
        return True
    return count < MAX_FAILS


def record_login_fail(ip: str):
    now = time.time()
    count, start = _login_fail.get(ip, (0, now))
    if now - start > FAIL_WINDOW_SEC:
        _login_fail[ip] = (1, now)
    else:
        _login_fail[ip] = (count + 1, start)


def clear_login_fail(ip: str):
    _login_fail.pop(ip, None)


def extract_token_from_headers(authorization: Optional[str], cookie_token: Optional[str]) -> Optional[str]:
    if authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip() or None
    if cookie_token:
        return cookie_token.strip() or None
    return None
