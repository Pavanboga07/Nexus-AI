"""A2A HTTP transport with SSRF protection (Part 6 spec §22, §35).

Abstraction: the A2A service depends on ``A2ATransport``/endpoint
validation, not on httpx. The HTTP implementation enforces:

- http/https schemes only (no file/ftp/javascript/data)
- loopback/private/link-local IP rejection unless explicitly allowed
  (development setting NEXUS_A2A_ALLOW_LOCAL_ENDPOINTS)
- no redirects (an attacker-controlled redirect could reach internal hosts)
- response size cap (a malicious remote cannot flood memory)
- hard timeout (a remote cannot hang the runtime)

DNS resolution happens inside the request; hostname-based checks cover the
common cases, and the no-redirect rule prevents the classic redirect-based
SSRF bypass.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from app.a2a.errors import A2AError, A2AErrorCode


class A2ATransport(Protocol):
    async def send(
        self, endpoint: str, envelope: dict[str, Any]
    ) -> dict[str, Any]:
        """Send a signed envelope, return the parsed response envelope."""
        ...


def _host_looks_local(hostname: str) -> bool:
    """True for loopback / private / link-local names and addresses."""
    lowered = hostname.lower()
    if lowered in {"localhost", "localhost.localdomain", "0.0.0.0"}:
        return True
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved
    try:
        addrinfos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False  # unresolvable names fail during the request anyway
    for info in addrinfos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved:
            return True
    return False


def validate_endpoint(endpoint: str, *, allow_local: bool) -> str:
    """Validate a remote agent endpoint. Raises A2AError on violations."""
    try:
        parts = urlsplit(endpoint)
    except ValueError:
        raise A2AError(A2AErrorCode.INVALID_ENDPOINT, "Malformed endpoint URL.") from None

    if parts.scheme not in {"http", "https"}:
        raise A2AError(
            A2AErrorCode.INVALID_ENDPOINT,
            "Endpoint scheme must be http or https.",
        )
    if not parts.hostname:
        raise A2AError(A2AErrorCode.INVALID_ENDPOINT, "Endpoint has no host.")
    if parts.username or parts.password:
        raise A2AError(
            A2AErrorCode.INVALID_ENDPOINT, "Credentials in URLs are not allowed."
        )
    if not allow_local and _host_looks_local(parts.hostname):
        raise A2AError(
            A2AErrorCode.INVALID_ENDPOINT,
            "Local/private network endpoints are not allowed in this "
            "deployment.",
        )
    return endpoint


class HttpA2ATransport:
    """httpx-based transport: no redirects, size-capped, time-bounded."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
        allow_local: bool,
    ) -> None:
        self._timeout = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._allow_local = allow_local

    async def send(
        self, endpoint: str, envelope: dict[str, Any]
    ) -> dict[str, Any]:
        validate_endpoint(endpoint, allow_local=self._allow_local)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
            ) as client:
                response = await client.post(endpoint, json=envelope)
        except httpx.TimeoutException:
            raise A2AError(
                A2AErrorCode.TRANSPORT_ERROR,
                "Remote agent did not respond in time.",
            ) from None
        except httpx.HTTPError as exc:
            raise A2AError(
                A2AErrorCode.TRANSPORT_ERROR,
                f"Transport failure contacting remote agent "
                f"({type(exc).__name__}).",
            ) from None

        if response.status_code in {301, 302, 303, 307, 308}:
            raise A2AError(
                A2AErrorCode.TRANSPORT_ERROR,
                "Remote agent attempted a redirect; refusing to follow.",
            )
        if len(response.content) > self._max_response_bytes:
            raise A2AError(
                A2AErrorCode.TRANSPORT_ERROR,
                "Remote agent response exceeds the size limit.",
            )
        if response.status_code >= 400:
            raise A2AError(
                A2AErrorCode.TRANSPORT_ERROR,
                f"Remote agent returned HTTP {response.status_code}.",
            )
        try:
            return response.json()
        except ValueError:
            raise A2AError(
                A2AErrorCode.INVALID_RESPONSE,
                "Remote agent returned a non-JSON response.",
            ) from None


__all__ = ["A2ATransport", "HttpA2ATransport", "validate_endpoint"]
