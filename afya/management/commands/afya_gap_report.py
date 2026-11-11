"""Anonymised district service-gap report for health planners.

    python manage.py afya_gap_report --days 90 --out gaps.csv
"""

import csv
import sys
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from afya.analytics import attendance_rate, district_report, voucher_alerts


class Command(BaseCommand):
    help = "Write an anonymised CSV of demand and unmet demand by district."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=90)
        parser.add_argument("--out", help="CSV file to write (default: print to screen)")
        parser.add_argument("--alerts", action="store_true", help="Also list voucher misuse alerts")

    def handle(self, *args, **options):
        since = timezone.localdate() - timedelta(days=options["days"])
        rows = district_report(since)
        fields = ["region", "district", "need", "requests", "unmatched", "gap_rate",
                  "booked", "danger_signs"]
        handle = open(options["out"], "w", newline="") if options["out"] else sys.stdout
        try:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        finally:
            if options["out"]:
                handle.close()
        rate = attendance_rate(since)
        self.stderr.write(f"Attendance rate: {rate:.0%}" if rate is not None
                          else "Attendance rate: no finished bookings yet")
        if options["alerts"]:
            for alert in voucher_alerts():
                self.stderr.write(f"ALERT: {alert}")
