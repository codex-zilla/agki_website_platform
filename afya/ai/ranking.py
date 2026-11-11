"""Matching a girl or woman to the best verified providers for her need.

Every candidate must be verified and must actually offer the service. Among
those, each is scored on five factors, weighted by `AFYA_RANKING_WEIGHTS`:

* distance - GPS distance when both sides have coordinates, otherwise same
  district > same region > elsewhere;
* cost - cheaper is better, free is best, a voucher can make it free;
* rating - a Bayesian average, so one 5-star review doesn't beat fifty 4s;
* youth-friendliness - only counts when she is an adolescent;
* emergency readiness - only counts for pregnancy-related needs.

The score is explainable: each match carries the reasons it ranked well, which
is what a girl sees ("karibu nawe, bei nafuu").
"""

import math
from dataclasses import dataclass, field

from django.conf import settings

from afya.models import Need, Provider, Service

DEFAULT_WEIGHTS = {
    "distance": 0.40,
    "cost": 0.20,
    "rating": 0.20,
    "youth": 0.10,
    "emergency": 0.10,
}

#: Above this price a service scores zero on cost.
COST_CEILING_TSH = 50_000
#: Bayesian prior: every provider starts as if it had this many reviews of this score.
PRIOR_REVIEWS = 5
PRIOR_SCORE = 3.5

PREGNANCY_NEEDS = {Need.ANTENATAL, Need.POSTNATAL}


@dataclass
class Match:
    provider: Provider
    service: Service
    score: float
    distance_km: float = None
    reasons: list = field(default_factory=list)


def haversine_km(lat1, lon1, lat2, lon2):
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def distance_score(provider, region, district, location=None):
    if location and provider.latitude is not None and provider.longitude is not None:
        km = haversine_km(location[0], location[1], provider.latitude, provider.longitude)
        return 1.0 / (1.0 + km / 5.0), km
    if district and provider.district.lower() == district.lower():
        return 1.0, None
    if region and provider.region.lower() == region.lower():
        return 0.6, None
    return 0.1, None


def cost_score(price, voucher_value=0):
    payable = max(price - voucher_value, 0)
    return 1.0 - min(payable, COST_CEILING_TSH) / COST_CEILING_TSH


def rating_score(provider):
    total = provider.rating_total + PRIOR_SCORE * PRIOR_REVIEWS
    count = provider.rating_count + PRIOR_REVIEWS
    return (total / count) / 5.0


def rank(need, region="", district="", *, location=None, adolescent=False,
         voucher_value=0, limit=3):
    """Best verified providers for `need`, best first."""
    weights = {**DEFAULT_WEIGHTS, **getattr(settings, "AFYA_RANKING_WEIGHTS", {})}
    services = (
        Service.objects.filter(
            need=need, is_active=True,
            provider__verification=Provider.Verification.VERIFIED,
        )
        .select_related("provider")
    )
    # Only look outside her region if nothing inside it offers the service.
    if region:
        local = services.filter(provider__region__iexact=region)
        if local.exists():
            services = local

    best = {}
    for service in services:
        provider = service.provider
        d_score, km = distance_score(provider, region, district, location)
        c_score = cost_score(service.price_tsh, voucher_value)
        r_score = rating_score(provider)
        y_score = 1.0 if (adolescent and provider.is_youth_friendly) else 0.0
        e_score = 1.0 if (need in PREGNANCY_NEEDS and provider.handles_emergencies) else 0.0
        score = (
            weights["distance"] * d_score + weights["cost"] * c_score
            + weights["rating"] * r_score + weights["youth"] * y_score
            + weights["emergency"] * e_score
        )
        reasons = []
        if d_score >= 0.6:
            reasons.append("near")
        if service.price_tsh == 0 or c_score >= 0.9:
            reasons.append("free" if service.price_tsh == 0 else "affordable")
        if provider.rating_count >= 3 and r_score >= 0.8:
            reasons.append("well_rated")
        if y_score:
            reasons.append("youth_friendly")
        match = Match(provider, service, round(score, 4), km, reasons)
        # One line per provider: keep its best-scoring service for this need.
        current = best.get(provider.pk)
        if current is None or match.score > current.score:
            best[provider.pk] = match

    return sorted(best.values(), key=lambda m: (-m.score, m.provider.name))[:limit]


def emergency_providers(region="", district="", limit=2):
    """Verified providers that take emergencies, nearest first."""
    providers = Provider.objects.filter(
        verification=Provider.Verification.VERIFIED, handles_emergencies=True
    )
    scored = [(distance_score(p, region, district)[0], p) for p in providers]
    scored.sort(key=lambda item: (-item[0], item[1].name))
    return [p for score, p in scored if score >= 0.6][:limit]
