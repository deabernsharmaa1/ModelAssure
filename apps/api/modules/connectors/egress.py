"""Outbound network controls (spec §12.2 "Testing isolation", OWASP API10 unsafe consumption).

ModelAssure only ever calls endpoints that are BOTH
  * on the platform allowlist (MA_ALLOWED_TEST_HOSTS), and
  * either on that list explicitly or on the organisation's approved destinations,
and never cloud metadata / link-local / multicast / unspecified addresses, whatever the allowlist
says. Hosts are re-resolved before each run to limit DNS-rebinding. Redirects are never followed.
ModelAssure never discovers endpoints: it calls only what an authorised engineer registered.
"""
from __future__ import annotations

import fnmatch
import ipaddress
import socket
from urllib.parse import urlsplit

from settings import get_settings

BLOCKED_NETWORKS = [
    ipaddress.ip_network("169.254.0.0/16"),     # link-local incl. 169.254.169.254 metadata
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fd00:ec2::/32"),      # AWS IPv6 metadata
    ipaddress.ip_network("100.100.100.200/32"), # Alibaba metadata
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("ff00::/8"),
    ipaddress.ip_network("::/128"),
]
BLOCKED_HOSTNAMES = {"metadata.google.internal", "metadata", "instance-data"}


class EgressError(ValueError):
    pass


def _host_allowed(host: str, patterns: set[str]) -> bool:
    return any(fnmatch.fnmatch(host, p) for p in patterns)


def check_url(url: str, org_destinations: list[str] | None = None, resolve: bool = True) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise EgressError("only http(s) endpoints are allowed")
    if parts.username or parts.password:
        raise EgressError("credentials must not be embedded in the URL; use a credential reference")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise EgressError("endpoint has no host")
    s = get_settings()
    if s.environment == "production" and parts.scheme != "https":
        raise EgressError("https is required in production")
    if host in BLOCKED_HOSTNAMES:
        raise EgressError(f"host '{host}' is blocked")
    platform = s.allowed_hosts
    org = {d.lower() for d in (org_destinations or [])}
    if not _host_allowed(host, platform):
        raise EgressError(f"host '{host}' is not on the platform allowlist of approved test destinations")
    if org and not _host_allowed(host, org) and host not in platform:
        raise EgressError(f"host '{host}' is not an approved destination for this organisation")
    if resolve:
        try:
            infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
        except socket.gaierror as e:
            raise EgressError(f"cannot resolve '{host}': {e}") from None
        for info in infos:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
            if any(ip in n for n in BLOCKED_NETWORKS):
                raise EgressError(f"host '{host}' resolves to blocked address {ip}")
    return url
