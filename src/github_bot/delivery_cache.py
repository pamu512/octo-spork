"""Idempotent GitHub webhook handling via Redis (``X-GitHub-Delivery``)."""

from __future__ import annotations

import re

from redis.asyncio import Redis

# GitHub sends a UUID for ``X-GitHub-Delivery``.
_DELIVERY_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

_DELIVERY_KEY_PREFIX = "github:webhook:delivery:"
_DELIVERY_TTL_SECONDS = 86_400  # 24 hours


def is_valid_delivery_id(delivery_id: str) -> bool:
    return bool(delivery_id and _DELIVERY_ID_RE.fullmatch(delivery_id.strip()))


async def try_claim_delivery(redis: Redis, delivery_id: str) -> bool:
    """Atomically record this delivery ID.

    Uses ``SET key NX EX`` so only the first request wins.

    Returns:
        ``True`` if this is the first time we see this delivery (caller should process).
        ``False`` if the ID was already recorded (duplicate webhook).
    """
    key = _DELIVERY_KEY_PREFIX + delivery_id.strip()
    # redis-py: returns True if SET succeeded, None if NX prevented set (duplicate).
    result = await redis.set(key, "1", nx=True, ex=_DELIVERY_TTL_SECONDS)
    return result is True
