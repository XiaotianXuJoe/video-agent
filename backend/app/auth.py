"""单用户 token 鉴权（C-SEC-01）。

通过条件（任一）：
- `Authorization: Bearer <token>`（脚本 / 前端 fetch）
- Cookie `va_token`（登录时 HttpOnly Set-Cookie；`<img>`/`<video>`/EventSource 等带不了请求头）
- `?token=<token>`（SSE 兜底）

token 为空（未配置）时，除 `/api/auth/login` 外所有 `/api/*` 一律 401，返回
`{"error": {"kind": "auth_unconfigured"}}`。静态资源（前端 SPA、/assets）不拦——
登录页必须能加载。
"""
import hmac
import os
import threading
import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from . import store

COOKIE_NAME = "va_token"
COOKIE_MAX_AGE = 30 * 24 * 3600  # 30 天
LOGIN_PATH = "/api/auth/login"
LOGIN_RATE_LIMIT = 10  # 每 IP 每分钟最多尝试次数
LOGIN_RATE_WINDOW = 60.0

_attempts: dict[str, deque] = defaultdict(deque)
_rl_lock = threading.Lock()


def configured_token() -> str:
    """当前访问密码（token）。优先环境变量 VA_AUTH_TOKEN（便于首次部署/未配置时引导），
    否则取 settings.auth.token。空串 = 未配置。"""
    env_token = (os.environ.get("VA_AUTH_TOKEN") or "").strip()
    if env_token:
        return env_token
    return ((store.load_settings().get("auth") or {}).get("token") or "").strip()


def token_matches(supplied: str, expected: str) -> bool:
    """恒定时间比较；任一为空直接 False（避免空 token 放行）。"""
    if not supplied or not expected:
        return False
    try:
        return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
    except TypeError:  # compare_digest 对非 str/bytes 才抛，这里防御式兜底
        return False


def _extract_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie:
        return cookie
    return request.query_params.get("token", "") or ""


class AuthMiddleware(BaseHTTPMiddleware):
    """只拦 /api/*；/api/auth/login 永远放行（登录本身不能要求已登录）。"""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/") or path == LOGIN_PATH:
            return await call_next(request)
        expected = configured_token()
        if not expected:
            return JSONResponse({"error": {"kind": "auth_unconfigured"}}, status_code=401)
        if token_matches(_extract_token(request), expected):
            return await call_next(request)
        return JSONResponse({"error": {"kind": "auth_required"}}, status_code=401)


def login_rate_limited(ip: str) -> bool:
    """简单内存限频：每 IP 每分钟 LOGIN_RATE_LIMIT 次。进程重启即清零，够用。"""
    now = time.time()
    with _rl_lock:
        q = _attempts[ip]
        while q and now - q[0] > LOGIN_RATE_WINDOW:
            q.popleft()
        if len(q) >= LOGIN_RATE_LIMIT:
            return True
        q.append(now)
        return False


def reset_rate_limit():
    """测试用：清空限频计数。"""
    with _rl_lock:
        _attempts.clear()
