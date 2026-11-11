"""Client IP resolution that understands trusted proxies.

Behind an ingress, ALB or nginx - the documented deployment target - every request
arrives from the proxy's address, so `request.client.host` is the proxy for every
client. Using it as the rate-limit key turns a per-client limit into a global one for
the whole cluster, and lets one noisy client exhaust the budget for everyone.

`X-Forwarded-For` cannot simply be trusted: a client can send any value it likes. The
header is therefore only read when the immediate peer is a configured trusted proxy,
and the address taken from it is the right-most one that is not itself a trusted
proxy - the address the outermost trusted hop observed.

With no trusted proxies configured the peer address is used unchanged, which is the
correct behaviour for a directly exposed deployment.
"""
from __future__ import annotations

from collections.abc import Iterable
from ipaddress import ip_address

from starlette.requests import Request

from blackbox.iputils import Network, is_trusted, strip_port


def resolve_client_ip(request: Request, trusted: Iterable[Network]) -> str:
    """The address the rate limiter should key on."""
    peer = request.client.host if request.client else None
    if not peer:
        return "unknown"

    networks = list(trusted)
    if not networks:
        return peer

    if not is_trusted(peer, networks):
        # The peer is not a proxy we trust, so any forwarded header it sent is
        # client-controlled and must be ignored.
        return peer

    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded:
        return peer

    candidates = [part.strip() for part in forwarded.split(",") if part.strip()]
    for candidate in reversed(candidates):
        address = strip_port(candidate)
        try:
            parsed = ip_address(address)
        except ValueError:
            continue
        if not is_trusted(str(parsed), networks):
            return str(parsed)
    return peer
