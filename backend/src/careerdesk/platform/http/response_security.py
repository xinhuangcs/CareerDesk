"""Privacy and browser response-header boundary, excluding HTTPS-only HSTS."""

from collections.abc import Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send


CONTENT_SECURITY_POLICY = "frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
STRICT_OFFLINE_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; connect-src 'self'; img-src 'self' data: blob:; "
    "style-src 'self' 'unsafe-inline'; script-src 'self'; font-src 'self' data:; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
)

_CACHE_CONTROL = b"cache-control"
_PRAGMA = b"pragma"
_CSP = b"content-security-policy"
_GLOBAL_SINGLE_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-dns-prefetch-control", b"off"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"cross-origin-resource-policy", b"same-origin"),
)


def _replace_header(
    headers: list[tuple[bytes, bytes]],
    name: bytes,
    replacements: list[tuple[bytes, bytes]],
) -> list[tuple[bytes, bytes]]:
    """Replace at the first matching header, remove duplicates, or append."""
    output: list[tuple[bytes, bytes]] = []
    inserted = False
    for header_name, value in headers:
        if header_name.lower() != name:
            output.append((header_name, value))
            continue
        if not inserted:
            output.extend(replacements)
            inserted = True
    if not inserted:
        output.extend(replacements)
    return output


def _ensure_list_directive(
    headers: list[tuple[bytes, bytes]],
    name: bytes,
    directive: str,
) -> list[tuple[bytes, bytes]]:
    """Merge comma-separated duplicates while preserving stricter directives."""
    existing = [
        value.decode("latin-1").strip()
        for header_name, value in headers
        if header_name.lower() == name and value.strip()
    ]
    combined = ", ".join(existing)
    has_directive = any(
        part.strip().lower() == directive
        for value in existing
        for part in value.split(",")
    )
    if not has_directive:
        combined = f"{combined}, {directive}" if combined else directive
    replacement = [(name, combined.encode("latin-1"))]
    return _replace_header(headers, name, replacement)


def _normalized_csp(value: bytes) -> str:
    """Recognize only this middleware's equivalent policy, never arbitrary CSP."""
    decoded = value.decode("latin-1")
    directives = [" ".join(item.split()).lower() for item in decoded.split(";") if item.strip()]
    return "; ".join(directives)


def _ensure_csp(
    headers: list[tuple[bytes, bytes]],
    *,
    policy: str,
) -> list[tuple[bytes, bytes]]:
    """Preserve CSP intersection semantics, deduplicating only our policy."""
    required = policy.encode("ascii")
    normalized_required = _normalized_csp(required)
    output: list[tuple[bytes, bytes]] = []
    required_seen = False
    for name, value in headers:
        if name.lower() != _CSP or _normalized_csp(value) != normalized_required:
            output.append((name, value))
            continue
        if not required_seen:
            output.append((_CSP, required))
            required_seen = True
    if not required_seen:
        output.append((_CSP, required))
    return output


def _secure_headers(
    headers: list[tuple[bytes, bytes]],
    *,
    api_response: bool,
    strict_offline: bool,
) -> list[tuple[bytes, bytes]]:
    output = list(headers)
    if api_response:
        output = _ensure_list_directive(output, _CACHE_CONTROL, "no-store")
        output = _ensure_list_directive(output, _PRAGMA, "no-cache")
    for name, value in _GLOBAL_SINGLE_HEADERS:
# Target values are strict and interoperable; normalizing weaker duplicates cannot loosen them.
        output = _replace_header(output, name, [(name, value)])
    return _ensure_csp(
        output,
        policy=(
            STRICT_OFFLINE_CONTENT_SECURITY_POLICY
            if strict_offline
            else CONTENT_SECURITY_POLICY
        ),
    )


class ResponseSecurityMiddleware:
    """Inject privacy/isolation headers without touching request or response bodies."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        strict_offline: bool | Callable[[], bool] = False,
    ) -> None:
        self.app = app
        self._strict_offline = strict_offline

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        api_response = path == "/api" or path.startswith("/api/")

        async def secure_send(message: Message) -> None:
            if message.get("type") == "http.response.start":
                secured = dict(message)
                secured["headers"] = _secure_headers(
                    list(message.get("headers", [])),
                    api_response=api_response,
                    strict_offline=(
                        self._strict_offline()
                        if callable(self._strict_offline)
                        else self._strict_offline
                    ),
                )
                await send(secured)
                return
            await send(message)

        await self.app(scope, receive, secure_send)


__all__ = [
    "CONTENT_SECURITY_POLICY",
    "STRICT_OFFLINE_CONTENT_SECURITY_POLICY",
    "ResponseSecurityMiddleware",
]
