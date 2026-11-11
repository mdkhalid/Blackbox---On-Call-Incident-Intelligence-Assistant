"""IP address helpers shared by the configuration and the API layer.

Kept at the package root so neither the config module nor the API layer has to
depend on the other for a parsing routine.
"""
from __future__ import annotations

from collections.abc import Iterable
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network

# `ipaddress.ip_network` is a factory, not a type, so the union is spelled out here.
Network = IPv4Network | IPv6Network


def parse_trusted_networks(cidrs: Iterable[str]) -> list[Network]:
    """Parse trusted proxy CIDRs, ignoring entries that are not valid networks."""
    networks: list[Network] = []
    for raw in cidrs:
        entry = (raw or "").strip()
        if not entry:
            continue
        try:
            networks.append(ip_network(entry, strict=False))
        except ValueError:
            continue
    return networks


def strip_port(address: str) -> str:
    """`1.2.3.4:5678` and `[::1]:5678` both carry a port; drop it."""
    if address.startswith("["):
        closing = address.find("]")
        if closing != -1:
            return address[1:closing]
        return address
    if address.count(":") == 1:
        return address.split(":", 1)[0]
    return address


def is_trusted(address: str, networks: Iterable[Network]) -> bool:
    try:
        parsed = ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in networks)
