"""Same-origin trust boundary applied before parsing or buffering write bodies."""

from collections.abc import Sequence
from ipaddress import ip_address
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

from .problem_details import send_problem


WRITE_REQUEST_HEADER = "X-CareerDesk-Request"
WRITE_REQUEST_VALUE = "1"

_WRITE_HEADER_BYTES = WRITE_REQUEST_HEADER.lower().encode("ascii")
_PROTECTED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_RUNTIME_MODES = {"development", "desktop", "server", "test"}
_FETCH_SITES = {"same-origin", "same-site", "cross-site", "none"}

Origin = tuple[str, str, int]


class _Headers:
    """Preserve duplicate headers so conflicting security signals are not hidden."""

    def __init__(self, scope: Scope) -> None:
        values: dict[bytes, list[str]] = {}
        for raw_name, raw_value in scope.get("headers", []):
            name = raw_name.lower()
            values.setdefault(name, []).append(raw_value.decode("latin-1"))
        self._values = values

    def get_all(self, name: bytes) -> tuple[str, ...]:
        return tuple(self._values.get(name, ()))

    @property
    def fetch_metadata_names(self) -> tuple[bytes, ...]:
        return tuple(name for name in self._values if name.startswith(b"sec-fetch-"))


def _origin(value: str, *, allow_resource_path: bool) -> Origin | None:
    """Normalize an HTTP(S) origin, treating explicit default ports as equivalent."""
    if (
        not value
        or value != value.strip()
        or value == "null"
        or "\\" in value
        or "#" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return None
    if allow_resource_path:
        # Referer may contain path/query. Fragments are never valid on the wire.
        pass
    elif parsed.path or parsed.query or "?" in value:
        # Browser-serialized Origin has no trailing slash, path, or query.
        return None
    canonical_port = port if port is not None else (443 if parsed.scheme.lower() == "https" else 80)
    return parsed.scheme.lower(), parsed.hostname.lower(), canonical_port


def _configured_origin(value: str) -> Origin:
    """Validate the startup allowlist, permitting a trailing slash but no path."""
    if (
        not value
        or value != value.strip()
        or value == "null"
        or "\\" in value
        or "?" in value
        or "#" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"非法可信 Origin：{value!r}")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"非法可信 Origin：{value!r}") from error
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"非法可信 Origin：{value!r}")
    canonical_port = port if port is not None else (443 if parsed.scheme.lower() == "https" else 80)
    return parsed.scheme.lower(), parsed.hostname.lower(), canonical_port


def _host_name(value: str) -> str | None:
    """Extract hostname from an already TrustedHost-validated Host value."""
    if not value or value != value.strip() or "\\" in value:
        return None
    try:
        parsed = urlsplit(f"//{value}")
        # Access port to trigger errors for invalid IPv6 or out-of-range values.
        parsed.port
    except ValueError:
        return None
    if (
        parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    return parsed.hostname.lower()


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


class RequestTrustMiddleware:
    """Reject untrusted browser writes to ``/api``.

    ``TrustedHostMiddleware`` must wrap this middleware. Desktop/development trust
    loopback aliases and differing ports for the Vite proxy; server/test accept
    only explicit origins. TestClient requests without browser-origin signals are
    allowed, but any Origin, Referer, or Fetch Metadata activates full policy.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        runtime_mode: str,
        allowed_origins: Sequence[str] = (),
    ) -> None:
        mode = str(getattr(runtime_mode, "value", runtime_mode))
        if mode not in _RUNTIME_MODES:
            raise ValueError(f"未知运行模式：{mode}")
        self.app = app
        self.runtime_mode = mode
        self.allowed_origins = frozenset(_configured_origin(item) for item in allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._protects(scope):
            await self.app(scope, receive, send)
            return

        headers = _Headers(scope)
        has_source_signal = bool(
            headers.get_all(b"origin")
            or headers.get_all(b"referer")
            or headers.fetch_metadata_names
        )
        write_header_values = headers.get_all(_WRITE_HEADER_BYTES)
        trusted_test_client = (
            self.runtime_mode == "test" and not has_source_signal and not write_header_values
        )
        if not trusted_test_client and write_header_values != (WRITE_REQUEST_VALUE,):
            await self._reject(scope, receive, send)
            return
        if has_source_signal and not self._trusted_source(headers):
            await self._reject(scope, receive, send)
            return
        await self.app(scope, receive, send)

    @staticmethod
    def _protects(scope: Scope) -> bool:
        if scope.get("type") != "http" or scope.get("method", "").upper() not in _PROTECTED_METHODS:
            return False
        path = scope.get("path", "")
        return path == "/api" or path.startswith("/api/")

    def _trusted_source(self, headers: _Headers) -> bool:
        origin_values = headers.get_all(b"origin")
        referer_values = headers.get_all(b"referer")
        fetch_site_values = headers.get_all(b"sec-fetch-site")

        # Duplicate authoritative source headers create parser ambiguity; fail closed.
        if len(origin_values) > 1 or len(referer_values) > 1 or len(fetch_site_values) > 1:
            return False

        fetch_names = headers.fetch_metadata_names
        if fetch_names:
            # Browser Fetch Metadata must include one valid Sec-Fetch-Site value.
            if len(fetch_site_values) != 1 or fetch_site_values[0] not in _FETCH_SITES:
                return False
            if fetch_site_values[0] == "cross-site":
                return False

        if origin_values:
            parsed = _origin(origin_values[0], allow_resource_path=False)
            return parsed is not None and self._origin_allowed(parsed, headers)

        if referer_values:
            parsed = _origin(referer_values[0], allow_resource_path=True)
            return parsed is not None and self._origin_allowed(parsed, headers)

        # Without a URL source, only same-origin metadata proves the request came
        # from this app. Signal-free CLI requests were handled before this method.
        return fetch_site_values == ("same-origin",)

    def _origin_allowed(self, origin: Origin, headers: _Headers) -> bool:
        if self.runtime_mode in {"development", "desktop"}:
            host_values = headers.get_all(b"host")
            if len(host_values) != 1:
                return False
            host = _host_name(host_values[0])
            return host is not None and _is_loopback(host) and _is_loopback(origin[1])
        return origin in self.allowed_origins

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        await send_problem(
            scope,
            receive,
            send,
            status_code=403,
            detail="untrusted browser write request",
        )


__all__ = [
    "RequestTrustMiddleware",
    "WRITE_REQUEST_HEADER",
    "WRITE_REQUEST_VALUE",
]
