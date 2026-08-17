"""A small fixed-window rate limiter on Django's cache framework.

Design §8.2. Once the booking-ID lookup is the only route to a patient record,
it becomes the thing worth brute-forcing — 100,000 booking IDs per year is not
many, and the second verification field only helps if an attacker cannot try
it thousands of times.

**No new dependency** (pre-flight decision 0.5). `django-ratelimit` would have
needed the same shared-cache change to work at all, so it bought polish rather
than correctness. What matters is the backend: with a per-process cache, each
Passenger worker keeps its own counter and the effective limit is
`limit x workers`. So this deliberately does **not** use the `default` cache —
that one is in-process on purpose, because it holds rendered page content
where a free read matters more than a shared one. It uses the separate
database-backed `ratelimit` alias every worker sees.

**Fixed window, not a sliding one.** A caller can get up to 2x the limit
across a window boundary. That is a known and acceptable property here: the
point is to make brute force slow, not to meter an API precisely, and a
sliding window costs a sorted structure per key for no benefit at this scale.
"""

from django.conf import settings
from django.core.cache import caches

# Design §8.2's numbers.
LOOKUP_LIMIT = 5
LOOKUP_WINDOW = 10 * 60  # seconds


def _cache():
    # Looked up per call rather than at import, so `override_settings` in a
    # test actually takes effect.
    return caches[getattr(settings, "RATELIMIT_CACHE_ALIAS", "default")]


def client_ip(request):
    """The address to count against.

    `REMOTE_ADDR` only. `X-Forwarded-For` is attacker-controlled unless a
    trusted proxy is known to overwrite it, and this project has no such
    setting configured — honouring it would let anyone reset their own counter
    by inventing a header, which is worse than no limit because it looks like
    one.
    """
    return request.META.get("REMOTE_ADDR", "") or "unknown"


def hit(scope, identifier, limit, window):
    """Count one attempt. Returns True when the caller is **over** the limit.

    `cache.add` + `cache.incr` rather than get-then-set: `add` is atomic on
    every backend, so two simultaneous requests cannot both see "no counter
    yet" and each write a 1.
    """
    cache = _cache()
    key = f"ratelimit:{scope}:{identifier}"
    if cache.add(key, 1, window):
        return False  # first attempt in this window
    try:
        count = cache.incr(key)
    except ValueError:
        # The key expired between `add` and `incr`. Start a fresh window
        # rather than failing the request: this is a race, not an attack.
        cache.set(key, 1, window)
        return False
    return count > limit


def lookup_is_rate_limited(request):
    """Per-IP limit for `booking_id_lookup` (design §8.2)."""
    return hit("booking-lookup", client_ip(request), LOOKUP_LIMIT, LOOKUP_WINDOW)
