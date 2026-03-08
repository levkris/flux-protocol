import asyncio
import logging
import time
from typing import Optional

import aiohttp

from .constants import FEDERATION_CACHE_TTL, FEDERATED_RE

log = logging.getLogger("flux.federation")

_cache: dict[str, tuple[dict, float]] = {}


def parse_federated(address: str) -> Optional[tuple[str, str]]:
    m = FEDERATED_RE.match(address)
    if not m:
        return None
    return m.group(1), m.group(2)


def is_local(address: str, domain: str) -> bool:
    parsed = parse_federated(address)
    if not parsed:
        return False
    return parsed[1].lower() == domain.lower()


def _resolve_scheme(domain: str) -> str:
    """Use HTTPS by default, HTTP only for localhost or when port is specified (development)."""
    if "localhost" in domain.lower() or ":" in domain:
        return "http"
    return "https"


async def resolve_address(federated: str, local_domain: str, session: aiohttp.ClientSession) -> Optional[str]:
    parsed = parse_federated(federated)
    if not parsed:
        return federated

    username, domain = parsed

    if domain.lower() == local_domain.lower():
        return None

    cache_key = federated.lower()
    if cache_key in _cache:
        result, expires = _cache[cache_key]
        if time.time() < expires:
            log.debug(f"cache hit for {federated}")
            return result.get("flux_address")

    scheme = _resolve_scheme(domain)
    url = f"{scheme}://{domain}/federation/resolve/{username}"

    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("ok") and data.get("flux_address"):
                    _cache[cache_key] = (data, time.time() + FEDERATION_CACHE_TTL)
                    log.info(f"resolved {federated} → {data['flux_address']}")
                    return data["flux_address"]
            elif resp.status == 404:
                log.info(f"remote user not found: {federated}")
                return None
    except Exception as e:
        log.warning(f"federation resolve failed for {federated}: {e}")

    return None


async def fetch_remote_profile(federated: str, session: aiohttp.ClientSession) -> Optional[dict]:
    parsed = parse_federated(federated)
    if not parsed:
        return None

    username, domain = parsed
    scheme = _resolve_scheme(domain)
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