"""Deliver queued notifications (plan 3.x, runs from cron).

    * * * * * cd /home/<user>/app && /path/to/python manage.py send_pending_notifications

**Why a cron-driven queue rather than sending in the request.**

The obvious implementation calls `send_mail` while the patient waits for their
booking to submit. That couples two unrelated things: a mail server having a
slow afternoon makes booking feel broken, and a mail server that is *down*
makes booking actually fail, for a message that is not part of the booking.
It also gives a failed send nowhere to go — no retry, no record, nothing to
tell an administrator afterwards.

**Why not a worker process or a task queue.** Celery, RQ and friends need a
broker (Redis, RabbitMQ) and a long-running process. This is shared cPanel
hosting: no broker, and Passenger starts and kills application processes as it
pleases, so a background thread spawned in a request is a thread that may be
killed mid-send with the row still marked pending. Cron is the one scheduler
this host actually offers, and Phase 6's expiry command needs it anyway.

**Why not a service worker.** Service workers run in the *visitor's browser*.
They can show a Web Push notification to somebody who has the site open and has
granted permission — they cannot send email or SMS, and they do not run when
nobody is visiting. Worth revisiting only if AGKI later wants doctors to get
browser alerts on top of email, which is a different feature.

Safe to run concurrently with itself and safe to re-run: each row is claimed
with an atomic status transition inside `deliver()`, so two overlapping cron
runs cannot both send the same message. A run killed mid-send leaves its row
claimed; the next run releases anything held too long and tries again.
"""

from django.core.management.base import BaseCommand

from appointments.models import NotificationLog
from appointments.notifications import deliver, release_stale_claims


class Command(BaseCommand):
    help = "Deliver queued notifications. Intended to run from cron every minute."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help=(
                "Maximum messages to attempt in one run (default 50). Keeps a "
                "single run bounded so a cron tick cannot turn into a long job."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would be sent, without sending or touching a row.",
        )

    def handle(self, *args, **options):
        released = 0 if options["dry_run"] else release_stale_claims()
        if released:
            self.stdout.write(
                f"released {released} message(s) abandoned by an earlier run"
            )

        pending = NotificationLog.objects.filter(
            status=NotificationLog.Status.PENDING,
            attempts__lt=NotificationLog.MAX_ATTEMPTS,
        ).order_by("created_at")[: options["limit"]]

        if options["dry_run"]:
            for message in pending:
                self.stdout.write(
                    f"  would send {message.event} -> {message.recipient} "
                    f"({message.channel}, attempt {message.attempts + 1})"
                )
            self.stdout.write(f"{len(pending)} message(s) queued.")
            return

        sent = skipped = failed = 0
        for message in list(pending):
            if deliver(message):
                sent += 1
            else:
                # `deliver` returns False both for a failed send and for a row
                # another run claimed first; the row itself says which.
                message.refresh_from_db()
                if message.status == NotificationLog.Status.SENT:
                    skipped += 1
                else:
                    failed += 1

        if sent or failed:
            summary = f"sent {sent}, failed {failed}"
            self.stdout.write(self.style.SUCCESS(summary) if not failed else summary)

        exhausted = NotificationLog.objects.filter(
            status=NotificationLog.Status.FAILED
        ).count()
        if exhausted:
            # Said on every run, deliberately: a message that has run out of
            # attempts is somebody who was never told, and it should keep being
            # visible until a person looks at it.
            self.stdout.write(
                self.style.WARNING(
                    f"{exhausted} message(s) have exhausted their retries and "
                    f"will not be sent. Review them in the admin under "
                    f"Notification logs."
                )
            )

