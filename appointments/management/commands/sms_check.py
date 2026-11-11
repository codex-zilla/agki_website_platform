"""Report Beem SMS configuration, and optionally send one live message.

    python manage.py sms_check                 # report only, no network call
    python manage.py sms_check --to 0712345678 # send one real message

Beem's documented SMS endpoint does not provide a non-delivery test path. The
report is therefore deliberately network-free, while ``--to`` is explicit
about performing a live, billable submission.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from appointments.notifications import beem, site_channels, sms_is_configured

SAMPLE = "AGKI: test message from the booking system. No action needed."


def _masked(value):
    if not value:
        return "NOT SET"
    return "set, %d characters, ends %r" % (len(value), value[-4:])


class Command(BaseCommand):
    help = "Report Beem SMS configuration, and optionally send one live message."

    def add_arguments(self, parser):
        parser.add_argument(
            "--to",
            help="Send one real message to this number (0712345678 or 255712345678).",
        )
        parser.add_argument("--text", default=SAMPLE, help="Override the message body.")

    def handle(self, *args, **options):
        self._report()
        if not options["to"]:
            self.stdout.write("\nNothing sent. Add --to 0712345678 to send one live message.")
            return
        self._send(options["to"], options["text"])

    def _report(self):
        self.stdout.write(self.style.MIGRATE_HEADING("Gateway"))
        for label, value in (
            ("API key", _masked(getattr(settings, "BEEM_API_KEY", ""))),
            ("Secret key", _masked(getattr(settings, "BEEM_SECRET_KEY", ""))),
            ("Sender ID", getattr(settings, "BEEM_SENDER_ID", "") or "NOT SET"),
            ("Base URL", getattr(settings, "BEEM_BASE_URL", "")),
            ("Endpoint", beem._endpoint()),
        ):
            self.stdout.write("  %-12s %s" % (label + ":", value))

        self.stdout.write(self.style.MIGRATE_HEADING("\nDelivery"))
        self.stdout.write("  %-12s %s" % ("Channels:", ", ".join(site_channels())))
        self.stdout.write(
            "  %-12s %s" % ("Queueing:", getattr(settings, "NOTIFICATIONS_DELIVERY", "queued"))
        )
        self.stdout.write("  %-12s %s" % ("Site URL:", getattr(settings, "SITE_URL", "")))

        if not sms_is_configured():
            self.stdout.write(self.style.ERROR(
                "  No SMS will be queued: BEEM_API_KEY, BEEM_SECRET_KEY or "
                "BEEM_SENDER_ID is missing, so the system stays email-only."
            ))
        else:
            self.stdout.write(self.style.WARNING(
                "  Beem has no non-delivery test endpoint here. A --to send is "
                "live and may consume credit."
            ))

        site = getattr(settings, "SITE_URL", "")
        if "127.0.0.1" in site or "localhost" in site:
            self.stdout.write(self.style.WARNING(
                "  SITE_URL points at this machine, so links inside the text "
                "will not open on a phone."
            ))

        if getattr(settings, "NOTIFICATIONS_DELIVERY", "queued") == "queued":
            self.stdout.write(
                "  Queued delivery: booking writes the message and "
                "send_pending_notifications sends it from cron."
            )

    def _send(self, number, text):
        try:
            normalised = beem.normalise(number)
        except ValueError as unusable:
            raise CommandError(str(unusable)) from unusable

        self.stdout.write(self.style.MIGRATE_HEADING("\nSending live message"))
        self.stdout.write("  To:       %s" % normalised)
        self.stdout.write("  Endpoint: %s" % beem._endpoint())
        self.stdout.write("  Text:     %s" % text)
        self.stdout.write("  Length:   %d characters" % len(text))

        try:
            request_id = beem.send(normalised, text, reference="agki-sms-check")
        except beem.SmsError as refused:
            raise CommandError("Beem refused it: %s" % refused) from refused

        self.stdout.write(self.style.SUCCESS(
            "\nAccepted by Beem, request id %s. Accepted is not the same as delivered."
            % request_id
        ))
