"""Delete what Afya Mfukoni holds without a reason to keep it.

Run once a day from cron:
    python manage.py afya_cleanup

* A number that wrote once but never agreed to the privacy terms keeps a
  `Client` row (and the first message, waiting for consent). After
  `--unconsented-hours` it is deleted, so "consent before we keep anything"
  stays true for people who simply never answered.
* Anonymous web visitors who never booked are deleted after `--web-days`.
* Conversation steps (a half-finished OTP, a list of clinics) older than
  `--stale-days` are cleared, so an old "2" can't book from an old list.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from afya.models import Client, Conversation


class Command(BaseCommand):
    help = "Delete unconsented numbers, idle web visitors and stale conversation state."

    def add_arguments(self, parser):
        parser.add_argument("--unconsented-hours", type=int, default=24)
        parser.add_argument("--web-days", type=int, default=30)
        parser.add_argument("--stale-days", type=int, default=2)

    def handle(self, *args, **options):
        now = timezone.now()
        unconsented = Client.objects.filter(
            consented_at__isnull=True, bookings__isnull=True,
            created_at__lt=now - timedelta(hours=options["unconsented_hours"]),
        )
        idle_web = Client.objects.filter(
            phone__startswith="web:", bookings__isnull=True,
            last_seen_at__lt=now - timedelta(days=options["web_days"]),
        )
        removed = 0
        for queryset in (unconsented, idle_web):
            for client in queryset.distinct():
                client.erase()
                removed += 1
        stale = Conversation.objects.filter(
            updated_at__lt=now - timedelta(days=options["stale_days"])
        ).exclude(state={})
        cleared = stale.update(state={})
        self.stdout.write(f"Removed {removed} client record(s); cleared {cleared} "
                          f"stale conversation(s).")
