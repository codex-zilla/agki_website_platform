"""Send visit reminders, antenatal-care nudges and rating requests.

Run once a day from cron, at 09:00:
    python manage.py afya_send_reminders
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from afya import outbound
from afya.messages import say
from afya.models import Booking, Channel, Client, Conversation, Need, ReminderLog

ANTENATAL_EVERY_DAYS = 14
ANTENATAL_MAX_NUDGES = 6
#: Stop antenatal nudges this long after she told us she was pregnant: by then
#: she has given birth, and a pregnancy text on a shared phone is a disclosure.
PREGNANCY_RELEVANT_DAYS = 280
RATING_AFTER_HOURS = 2
#: Don't take over an SMS conversation she used within this time.
ACTIVE_CONVERSATION_HOURS = 2


class Command(BaseCommand):
    help = "Send Afya Mfukoni reminders by SMS."

    def handle(self, *args, **options):
        now = timezone.now()
        sent = {"visit": self.visits(now), "antenatal": self.antenatal(now),
                "rating": self.ratings(now)}
        self.stdout.write(", ".join(f"{k}: {v}" for k, v in sent.items()))

    def _send(self, client, kind, text, booking=None):
        outbound.send(client, text)
        ReminderLog.objects.create(client=client, kind=kind, booking=booking)

    def visits(self, now):
        tomorrow = timezone.localdate(now) + timedelta(days=1)
        count = 0
        due = Booking.objects.filter(status=Booking.Status.CONFIRMED, visit_at__date=tomorrow,
                                     client__isnull=False, client__reminders_opt_out=False) \
            .select_related("client", "provider")
        for booking in due:
            if ReminderLog.objects.filter(booking=booking, kind=ReminderLog.Kind.VISIT).exists():
                continue
            when = timezone.localtime(booking.visit_at).strftime("%H:%M")
            self._send(booking.client, ReminderLog.Kind.VISIT,
                       say("remind_visit", booking.client.language, ref=booking.reference,
                           when=when, provider=booking.provider.name), booking)
            count += 1
        return count

    def antenatal(self, now):
        """Nudge pregnant clients who haven't booked antenatal care recently."""
        count = 0
        clients = Client.objects.filter(
            is_pregnant=True, reminders_opt_out=False, consented_at__isnull=False,
            pregnancy_noted_at__gte=now - timedelta(days=PREGNANCY_RELEVANT_DAYS),
        ).exclude(phone__startswith="web:")
        for client in clients:
            if client.bookings.filter(service__need=Need.ANTENATAL,
                                      created_at__gte=now - timedelta(days=30)).exists():
                continue
            nudges = client.reminders.filter(kind=ReminderLog.Kind.ANTENATAL)
            if nudges.count() >= ANTENATAL_MAX_NUDGES:
                continue
            last = nudges.order_by("-sent_at").first()
            if last and last.sent_at > now - timedelta(days=ANTENATAL_EVERY_DAYS):
                continue
            self._send(client, ReminderLog.Kind.ANTENATAL, say("remind_antenatal", client.language))
            count += 1
        return count

    def ratings(self, now):
        count = 0
        done = Booking.objects.filter(status=Booking.Status.ATTENDED, rating__isnull=True,
                                      client__isnull=False,
                                      updated_at__lte=now - timedelta(hours=RATING_AFTER_HOURS),
                                      updated_at__gte=now - timedelta(days=3)) \
            .select_related("client", "provider")
        for booking in done:
            client = booking.client
            if client.reminders_opt_out or client.is_web_visitor:
                continue
            if ReminderLog.objects.filter(booking=booking, kind=ReminderLog.Kind.RATING).exists():
                continue
            conversation, _ = Conversation.objects.get_or_create(client=client, channel=Channel.SMS)
            if conversation.state and conversation.updated_at > \
                    now - timedelta(hours=ACTIVE_CONVERSATION_HOURS):
                continue  # she is in the middle of something; ask on the next run
            conversation.state = {"step": "rating", "booking": booking.pk}
            conversation.save(update_fields=["state", "updated_at"])
            self._send(client, ReminderLog.Kind.RATING,
                       say("ask_rating", client.language, provider=booking.provider.name), booking)
            count += 1
        return count
