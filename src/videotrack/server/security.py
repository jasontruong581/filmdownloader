"""Access control for a single-operator local server.

Two distinct protections, for two distinct threats:

**A wider bind needs a token.** The API resolves operator-supplied URLs
server-side and writes files to disk, so a reachable unauthenticated instance is
both an SSRF and a disk-write primitive. Binding off loopback without a token is
refused at startup rather than warned about.

**A loopback bind needs a Host check.** Loopback alone is not safe: a malicious
page can point its own hostname at 127.0.0.1 (DNS rebinding) and then reach this
API as same-origin, queueing arbitrary downloads. Requests whose Host header is
not a loopback name are rejected.
"""

from __future__ import annotations

import hmac
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

from .settings import LOOPBACK_HOSTS, Settings, token


class InsecureConfiguration(RuntimeError):
    """The requested bind cannot be served safely."""


def verify_configuration(settings: Settings, configured_token: str | None = None) -> None:
    """Refuse to start a configuration that cannot be protected."""
    configured_token = token() if configured_token is None else configured_token
    if not settings.is_loopback and not configured_token:
        raise InsecureConfiguration(
            f"Refusing to bind {settings.host}: a non-loopback bind requires a token. "
            "Set FILMDOWNLOADER_TOKEN, or bind 127.0.0.1."
        )


def _host_only(raw_host: str) -> str:
    if not raw_host:
        return ""
    # urlsplit needs a scheme to parse a host:port authority reliably, and it
    # handles bracketed IPv6 correctly where a naive rsplit does not.
    parsed = urlsplit(f"//{raw_host}")
    return (parsed.hostname or "").lower()


def host_is_allowed(raw_host: str, settings: Settings) -> bool:
    host = _host_only(raw_host)
    if not host:
        # A request with no Host header cannot be attributed; reject it.
        return False
    if settings.is_loopback:
        return host in LOOPBACK_HOSTS
    # A deliberately wider bind is reachable by whatever name resolves to it,
    # and the token is what protects it.
    return True


def make_guard(settings: Settings, configured_token: str | None = None):
    """Build the dependency that guards every /api request."""
    expected = token() if configured_token is None else configured_token

    async def guard(request: Request) -> None:
        if not host_is_allowed(request.headers.get("host", ""), settings):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "reason": "host_not_allowed",
                    "message": "This server only answers requests addressed to localhost.",
                },
            )

        if not expected:
            return

        supplied = request.headers.get("authorization", "")
        prefix = "bearer "
        if not supplied.lower().startswith(prefix):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"reason": "token_required", "message": "Provide a bearer token."},
            )
        if not hmac.compare_digest(supplied[len(prefix) :].strip(), expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"reason": "token_invalid", "message": "Token rejected."},
            )

    return guard
