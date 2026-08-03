"""Authenticate trusted gateway users with a shared-secret defense in depth.

Production requests first pass a deployer-selected login gateway, which injects
``Remote-User``. The backend never accepts client-declared identity in bodies or
queries. ``X-Gateway-Auth`` additionally prevents a directly exposed container
port from trusting a forged user header.

Desktop/development may fall back to ``dev_fake_user``. Server startup validation
requires debug off, a gateway secret, no fake user, and explicit Host/Origin
allowlists. Debug controls diagnostics only and is never a security input.
"""

import hmac
from urllib.parse import urlsplit

from fastapi import Header, HTTPException

from .core.config import get_settings


def current_user_id(
    remote_user: str | None = Header(default=None, alias="Remote-User"),
    gateway_secret: str | None = Header(default=None, alias="X-Gateway-Auth"),
) -> str:
    """Resolve the current user from gateway headers or a local fake user.

    Production verifies the shared secret before trusting ``Remote-User``.
    FastAPI matches the aliased headers case-insensitively.
    """
    settings = get_settings()
    expected = settings.gateway_auth_secret

    # Even without ASGI lifespan, server mode must never trust an unauthenticated
    # header or fall back to a fake user.
    if settings.runtime_mode == "server" and (
        expected is None or settings.dev_fake_user is not None
    ):
        raise HTTPException(status_code=503, detail="server authentication is not configured")

    if expected is not None:
        # Production requires a constant-time shared-secret match that proves the
        # request passed through the trusted gateway. Fake users never apply here.
        provided = gateway_secret or ""
        if not hmac.compare_digest(provided.encode(), expected.get_secret_value().encode()):
            raise HTTPException(status_code=401, detail="unauthorized")
    elif settings.dev_fake_user and not remote_user:
        # Local development without a gateway may use the explicit fake user.
        return settings.dev_fake_user

    if remote_user:
        return remote_user
    raise HTTPException(status_code=401, detail="unauthorized")


def verify_auth_config() -> None:
    """Fail-fast server authentication validation independent of debug mode."""
    settings = get_settings()
    if settings.runtime_mode != "server":
        return
    if settings.debug:
        raise RuntimeError(
            "server 模式必须配置 APP_DEBUG=false；debug 只供本机调试，"
            "公网实例不得暴露 OpenAPI 路由地图。"
        )
    if settings.gateway_auth_secret is None:
        raise RuntimeError(
            "server 模式必须配置 APP_GATEWAY_AUTH_SECRET（网关↔后端共享密钥）："
            "否则后端会无条件信任 Remote-User 头，存在被直连伪造身份越权读取数据的风险。"
            "本地运行请用 APP_RUNTIME_MODE=desktop 或 development。"
        )
    if settings.dev_fake_user is not None:
        raise RuntimeError(
            "server 模式禁止设置 APP_DEV_FAKE_USER："
            "它是本地无网关时的假登录旁路，残留到生产会绕过网关鉴权。请在生产 .env 里清空它。"
        )
    hosts = settings.allowed_host_list
    if not hosts or any(host == "*" or "*" in host or "://" in host or "/" in host for host in hosts):
        raise RuntimeError(
            "server 模式必须用 APP_ALLOWED_HOSTS 显式列出公网 Host，不允许 *、scheme 或路径。"
        )
    origins = settings.allowed_origin_list
    if not origins:
        raise RuntimeError(
            "server 模式必须用 APP_ALLOWED_ORIGINS 显式列出 HTTPS Origin。"
        )
    for origin in origins:
        parsed = urlsplit(origin)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or origin.rstrip("/") != origin):
            raise RuntimeError(
                "APP_ALLOWED_ORIGINS 每项必须是不带路径/查询/尾斜线的精确 HTTPS origin，"
                "例如 https://careerdesk.example.com。"
            )
