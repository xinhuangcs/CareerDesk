
from collections.abc import Sequence

from fastapi.testclient import TestClient

from careerdesk.platform.http.response_security import (
    CONTENT_SECURITY_POLICY,
    STRICT_OFFLINE_CONTENT_SECURITY_POLICY,
    ResponseSecurityMiddleware,
)


def _client(
    response_headers: Sequence[tuple[bytes, bytes]] = (),
    *,
    status: int = 200,
    strict_offline: bool = False,
) -> TestClient:
    async def downstream(scope, receive, send):
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": list(response_headers),
        })
        await send({"type": "http.response.body", "body": b"payload"})

    return TestClient(
        ResponseSecurityMiddleware(downstream, strict_offline=strict_offline),
    )


def _raw_values(response, name: str) -> list[str]:
    target = name.lower().encode("ascii")
    return [
        value.decode("latin-1")
        for header_name, value in response.headers.raw
        if header_name.lower() == target
    ]


def test_api_responses_disable_caching_and_add_global_browser_guards():
    response = _client().get("/api/private")

    assert response.status_code == 200 and response.content == b"payload"
    assert _raw_values(response, "Cache-Control") == ["no-store"]
    assert _raw_values(response, "Pragma") == ["no-cache"]
    assert _raw_values(response, "X-Content-Type-Options") == ["nosniff"]
    assert _raw_values(response, "X-DNS-Prefetch-Control") == ["off"]
    assert _raw_values(response, "Referrer-Policy") == ["no-referrer"]
    assert _raw_values(response, "X-Frame-Options") == ["DENY"]
    assert _raw_values(response, "Cross-Origin-Resource-Policy") == ["same-origin"]
    assert _raw_values(response, "Content-Security-Policy") == [CONTENT_SECURITY_POLICY]

    assert "strict-transport-security" not in response.headers
    assert not any(name.lower().startswith("access-control-") for name in response.headers)


def test_non_api_responses_get_browser_guards_without_forcing_private_cache_policy():
    for path in ("/", "/assets/app.js", "/apiary"):
        response = _client().get(path)
        assert "cache-control" not in response.headers
        assert "pragma" not in response.headers
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-dns-prefetch-control"] == "off"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["cross-origin-resource-policy"] == "same-origin"
        assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


def test_strict_offline_csp_blocks_remote_connections_and_passive_images():
    response = _client(strict_offline=True).get("/")

    assert response.headers["content-security-policy"] == STRICT_OFFLINE_CONTENT_SECURITY_POLICY
    assert "default-src 'self'" in STRICT_OFFLINE_CONTENT_SECURITY_POLICY
    assert "connect-src 'self'" in STRICT_OFFLINE_CONTENT_SECURITY_POLICY
    assert "img-src 'self' data: blob:" in STRICT_OFFLINE_CONTENT_SECURITY_POLICY


def test_existing_cache_directives_and_stricter_csp_are_preserved_deterministically():
    stronger_csp = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
    response = _client([
        (b"content-type", b"application/json"),
        (b"Cache-Control", b"private"),
        (b"cache-control", b"max-age=0"),
        (b"cache-control", b"no-store"),
        (b"Pragma", b"extension-token"),
        (b"pragma", b"no-cache"),
        (b"content-security-policy", stronger_csp.encode("ascii")),
        (b"Content-Security-Policy", CONTENT_SECURITY_POLICY.encode("ascii")),
        (b"content-security-policy", b" FRAME-ANCESTORS  'none' ; BASE-URI 'self'; OBJECT-SRC 'none' "),
        (b"set-cookie", b"one=1; HttpOnly"),
        (b"set-cookie", b"two=2; HttpOnly"),
    ]).get("/api/private")

    assert _raw_values(response, "Cache-Control") == ["private, max-age=0, no-store"]
    assert _raw_values(response, "Pragma") == ["extension-token, no-cache"]
    assert _raw_values(response, "Content-Security-Policy") == [
        stronger_csp,
        CONTENT_SECURITY_POLICY,
    ]
    assert _raw_values(response, "Set-Cookie") == ["one=1; HttpOnly", "two=2; HttpOnly"]


def test_weaker_or_duplicate_single_value_headers_collapse_to_strict_values():
    response = _client([
        (b"x-content-type-options", b"invalid"),
        (b"X-Content-Type-Options", b"nosniff"),
        (b"x-dns-prefetch-control", b"on"),
        (b"X-DNS-Prefetch-Control", b"off"),
        (b"referrer-policy", b"unsafe-url"),
        (b"Referrer-Policy", b"no-referrer"),
        (b"x-frame-options", b"SAMEORIGIN"),
        (b"X-Frame-Options", b"DENY"),
        (b"cross-origin-resource-policy", b"cross-origin"),
    ], status=403).get("/forbidden")

    assert response.status_code == 403
    assert _raw_values(response, "X-Content-Type-Options") == ["nosniff"]
    assert _raw_values(response, "X-DNS-Prefetch-Control") == ["off"]
    assert _raw_values(response, "Referrer-Policy") == ["no-referrer"]
    assert _raw_values(response, "X-Frame-Options") == ["DENY"]
    assert _raw_values(response, "Cross-Origin-Resource-Policy") == ["same-origin"]


def test_invalid_cache_directive_does_not_mask_required_valid_directive():
    response = _client([
        (b"cache-control", b"no-store=invalid"),
        (b"pragma", b"no-cache=invalid"),
    ]).get("/api")

    assert _raw_values(response, "Cache-Control") == ["no-store=invalid, no-store"]
    assert _raw_values(response, "Pragma") == ["no-cache=invalid, no-cache"]
