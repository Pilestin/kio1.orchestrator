"""Where a model server is, as far as safety is concerned.

KIO1 may plan with a model on this machine, on a private network — a GPU box
reached over Tailscale, say — or somewhere on the internet. The three call for
different care: only a server on this machine is KIO1's to start or to fill
with downloads; one on a shared private machine belongs to others too; and
plain HTTP across the internet hands every prompt — the user's request, the
code it quotes — to whoever is on the path.

The address is read, never looked up: a name could resolve differently
tomorrow, and the answer is only used to warn and to refuse downloads.
"""

from __future__ import annotations

import ipaddress
from typing import Literal
from urllib.parse import urlparse

Scope = Literal["loopback", "private", "public"]

# Tailscale hands out addresses from the carrier-grade NAT range.
_TAILSCALE = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


def scope(url: str) -> Scope:
    """`loopback` for this machine, `private` for a LAN or tailnet, else `public`.

    A bare name with no dot (`gpu-box`, as Tailscale's MagicDNS gives) and a
    `*.ts.net` name count as private; any other name as public, since nothing
    about it says otherwise.
    """
    host = (urlparse(url).hostname or "").lower()

    if host in ("localhost", "") or host.endswith(".localhost"):
        return "loopback"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "private" if "." not in host or host.endswith(".ts.net") else "public"

    if address.is_loopback:
        return "loopback"
    if address.is_private or address in _TAILSCALE or address in _TAILSCALE_V6:
        return "private"
    return "public"


def is_local(url: str) -> bool:
    return scope(url) == "loopback"


def exposed(url: str) -> bool:
    """Whether prompts would cross the internet unencrypted."""
    return scope(url) == "public" and urlparse(url).scheme != "https"
