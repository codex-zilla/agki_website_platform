"""Anonymised insight for health planners, and voucher misuse checks.

The district report is built only from `NeedEvent` (no people in it) and
suppresses any count below `SMALL_COUNT`, so a rare request in a small
district can't point to one person.
"""

from collections import Counter, defaultdict
from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from afya.models import Booking, NeedEvent, Voucher

SMALL_COUNT = 5


def _safe(number):
    return number if number >= SMALL_COUNT else f"<{SMALL_COUNT}"


def district_report(since, until=None):
    """Rows of demand, unmet demand and bookings per district and need."""
    until = until or timezone.localdate()
    events = NeedEvent.objects.filter(created_on__gte=since, created_on__lte=until)
    rows = (
        events.exclude(need="")
        .values("region", "district", "need")
        .annotate(
            requests=Count("id"),
            unmatched=Count("id", filter=Q(matched=False)),
            booked=Count("id", filter=Q(booked=True)),
            danger=Count("id", filter=~Q(danger_sign="")),
        )
        .order_by("region", "district", "-requests")
    )
    report = []
    for row in rows:
        gap = row["unmatched"] / row["requests"] if row["requests"] else 0
        report.append({
            "region": row["region"] or "Unknown",
            "district": row["district"] or "Unknown",
            "need": row["need"],
            "requests": _safe(row["requests"]),
            "unmatched": _safe(row["unmatched"]),
            "gap_rate": f"{gap:.0%}" if row["requests"] >= SMALL_COUNT else "-",
            "booked": _safe(row["booked"]),
            "danger_signs": _safe(row["danger"]),
        })
    return report


def attendance_rate(since):
    finished = Booking.objects.filter(created_at__date__gte=since,
                                      status__in=[Booking.Status.ATTENDED, Booking.Status.NO_SHOW])
    total = finished.count()
    attended = finished.filter(status=Booking.Status.ATTENDED).count()
    return (attended / total) if total else None


def voucher_alerts(days=30, per_phone_limit=3, provider_share_limit=0.5):
    """Patterns worth a human look. Returns a list of plain-text alerts."""
    since = timezone.now() - timedelta(days=days)
    alerts = []

    recent = Voucher.objects.filter(redeemed_at__gte=since)
    per_phone = Counter(v.claimed_phone_hash for v in recent if v.claimed_phone_hash)
    for fingerprint, count in per_phone.items():
        if count > per_phone_limit:
            alerts.append(f"One phone redeemed {count} vouchers in {days} days "
                          f"(fingerprint {fingerprint[:10]}).")

    by_sponsor = defaultdict(Counter)
    for booking in Booking.objects.filter(voucher__in=recent).select_related("voucher", "provider"):
        by_sponsor[booking.voucher.sponsor][booking.provider.name] += 1
    for sponsor, providers in by_sponsor.items():
        total = sum(providers.values())
        name, count = providers.most_common(1)[0]
        if total >= 10 and count / total > provider_share_limit:
            alerts.append(f"{name} redeemed {count} of {total} {sponsor} vouchers "
                          f"({count / total:.0%}) in {days} days.")

    for booking in Booking.objects.filter(voucher__in=recent, status=Booking.Status.NO_SHOW):
        alerts.append(f"Voucher {booking.voucher.code} was redeemed but booking "
                      f"{booking.reference} is marked no-show.")
    return alerts
