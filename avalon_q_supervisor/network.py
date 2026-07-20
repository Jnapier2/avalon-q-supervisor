"""Network-boundary validation shared by the CLI and transport."""

from __future__ import annotations

import ipaddress


_LOCAL_IPV4_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16")
)
_LOCAL_IPV6_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in ("fc00::/7", "::1/128", "fe80::/10")
)


def validate_local_address(host: str) -> None:
    """Require a literal address inside an explicitly local network range."""
    if type(host) is not str:
        raise ValueError("device host must be a string")
    try:
        address = ipaddress.ip_address(host.strip())
    except ValueError as exc:
        raise ValueError(
            "live device host must be a literal private, loopback, or link-local IP address"
        ) from exc
    allowed_networks = _LOCAL_IPV4_NETWORKS if address.version == 4 else _LOCAL_IPV6_NETWORKS
    if not any(address in network for network in allowed_networks):
        raise ValueError("live device host is outside the private, loopback, or link-local boundary")
