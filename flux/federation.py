import asyncio
import logging
import re
import time
from typing import Optional

import aiohttp

log = logging.getLogger("flux.federation")

# Cache resolved addresses so we don't hammer remote servers
_cache: dict[str, tuple[dict, float]] = {}
CACHE_TTL = 300  # seconds

# Pattern for federated addresses: user@domain or user@domain:port
FEDERATED_RE = re.compile(r"^([a-zA-Z0-9._-]+)@([a-zA-Z0-9._:-]+)$")


def parse_federated(address: str) -> Optional[tuple[str, str]]:
    """
    Parse 'user@domain' into (username, domain).
    Returns None if the address is a raw fx1... address.
    """
    m = FEDERATED_RE.match(address)
    if not m:
        return None
    return m.group(1), m.group(2)


def is_local(address: str, domain: str) -> bool:
    """Check if a federated address belongs to this server's domain."""
    parsed = parse_federated(address)
    if not parsed:
        return False
    return parsed[1].lower() == domain.lower()


async def resolve_address(federated: str, local_domain: str, session: aiohttp.ClientSession) -> Optional[str]:
    """
    Resolve 'user@domain' to a raw fx1... FLUX address.

    For local addresses: look up in local account store (caller handles this).
    For remote addresses: HTTP GET to the remote server's /federation/resolve/{username} endpoint.

    Returns the fx1 address string, or None if not found.
    """
    parsed = parse_federated(federated)
    if not parsed:
        # Already a raw fx1 address — nothing to resolve
        return federated

    username, domain = parsed

    if domain.lower() == local_domain.lower():
        # Signal to caller to do a local lookup
        return None

    # Check cache
    cache_key = federated.lower()
    if cache_key in _cache:
        result, expires = _cache[cache_key]
        if time.time() < expires:
            log.debug(f"cache hit for {federated}")
            return result.get("flux_address")

    # Ask the remote server
    scheme = "https" if ":" not in domain else "http"
    url = f"{scheme}://{domain}/federation/resolve/{username}"

    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("ok") and data.get("flux_address"):
                    _cache[cache_key] = (data, time.time() + CACHE_TTL)
                    log.info(f"resolved {federated} → {data['flux_address']}")
                    return data["flux_address"]
            elif resp.status == 404:
                log.info(f"remote user not found: {federated}")
                return None
    except Exception as e:
        log.warning(f"federation resolve failed for {federated}: {e}")

    return None


async def fetch_remote_profile(federated: str, session: aiohttp.ClientSession) -> Optional[dict]:
    """
    Fetch the full public profile of a remote user.
    Returns username, display_name, flux_address, flux_pub_b64.
    """
    parsed = parse_federated(federated)
    if not parsed:
        return None

    username, domain = parsed
    scheme = "https" if ":" not in domain else "http"
    url = f"{scheme}://{domain}/federation/resolve/{username}"

    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("ok"):
                    return data
    except Exception as e:
        log.warning(f"fetch_remote_profile failed for {federated}: {e}")

    return None
