"""Import providers and their services from a CSV file.

    python manage.py afya_import_providers afya/data/providers_tz_public.csv --dry-run
    python manage.py afya_import_providers afya/data/providers_tz_public.csv

The columns are described in `afya/data/README.md`. Every new provider is
saved as *Pending verification*: nothing imported reaches a woman until a
staff member checks it and uses "Mark verified" in the admin.

Re-importing is safe. Rows match on name + district; blank cells never wipe
what staff have already filled in; verification status is never changed;
existing services (and their confirmed prices) are left alone.
"""

import csv
from dataclasses import dataclass, field

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from afya.ai.language import PLACES, TOWN_TO_DISTRICT
from afya.messages import NEED_NAMES
from afya.models import Need, Provider, Service

REQUIRED = ("name", "kind", "region", "district", "council", "services")
YES = {"yes", "y", "true", "1", "ndiyo"}
NO = {"no", "n", "false", "0", "hapana", ""}
TEXT_FIELDS = ("address", "phone", "mobile_money_number", "licence_number", "opening_hours")


@dataclass
class Row:
    number: int
    provider: dict
    services: list = field(default_factory=list)  # [(need, price or None)]


def _flag(value, column, number):
    value = (value or "").strip().lower()
    if value in YES:
        return True
    if value in NO:
        return False
    raise ValueError(f"row {number}: {column} must be yes or no, not {value!r}")


def _region(value, number):
    for region in PLACES:
        if region.lower() == value.strip().lower():
            return region
    raise ValueError(f"row {number}: unknown region {value!r}")


def _district(region, value, number):
    for district in PLACES[region]:
        if district.lower() == value.strip().lower():
            return TOWN_TO_DISTRICT.get(district, district)
    if not PLACES[region]:  # region listed without districts: accept as written
        return value.strip()
    raise ValueError(f"row {number}: {value!r} is not a district of {region} "
                     f"(known: {', '.join(PLACES[region])})")


def _services(value, number):
    services = []
    for item in filter(None, (part.strip() for part in value.split(";"))):
        need, _, price = item.partition(":")
        need = need.strip().lower()
        if need not in Need.values:
            raise ValueError(f"row {number}: unknown service {need!r} "
                             f"(use one of: {', '.join(Need.values)})")
        price = price.strip()
        if price in ("?", ""):
            services.append((need, None))
        elif price.replace(",", "").isdigit():
            services.append((need, int(price.replace(",", ""))))
        else:
            raise ValueError(f"row {number}: price for {need} must be a number or ?, "
                             f"not {price!r}")
    if not services:
        raise ValueError(f"row {number}: at least one service is needed")
    return services


def parse(handle):
    """Read and check every row. Returns (rows, errors)."""
    reader = csv.DictReader(handle)
    missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
    if missing:
        raise CommandError(f"Missing column(s): {', '.join(missing)}")
    rows, errors = [], []
    for number, raw in enumerate(reader, start=2):  # row 1 is the header
        raw = {k: (v or "").strip() for k, v in raw.items() if k}
        if not any(raw.values()):
            continue
        try:
            for column in ("name", "kind", "region", "district", "council"):
                if not raw.get(column):
                    raise ValueError(f"row {number}: {column} is empty")
            if raw["kind"] not in Provider.Kind.values:
                raise ValueError(f"row {number}: kind must be one of {Provider.Kind.values}")
            if raw["council"] not in Provider.Council.values:
                raise ValueError(f"row {number}: council must be one of "
                                 f"{Provider.Council.values}")
            region = _region(raw["region"], number)
            provider = {
                "name": raw["name"][:150], "kind": raw["kind"], "council": raw["council"],
                "region": region, "district": _district(region, raw["district"], number),
                "is_youth_friendly": _flag(raw.get("is_youth_friendly"), "is_youth_friendly",
                                           number),
                "handles_emergencies": _flag(raw.get("handles_emergencies"),
                                             "handles_emergencies", number),
            }
            for column in TEXT_FIELDS:
                provider[column] = raw.get(column, "")
            rows.append(Row(number, provider, _services(raw["services"], number)))
        except ValueError as problem:
            errors.append(str(problem))
    return rows, errors


def _capital(text):
    return text[:1].upper() + text[1:]


def save(row):
    """Create or update one provider and add its missing services. Returns (created, added)."""
    data = dict(row.provider)
    provider = Provider.objects.filter(name__iexact=data["name"],
                                       district__iexact=data["district"]).first()
    created = provider is None
    if created:
        provider = Provider(verification=Provider.Verification.PENDING)
    for key, value in data.items():
        # A blank cell never wipes a phone or licence number staff typed in.
        if created or key not in TEXT_FIELDS or value:
            setattr(provider, key, value)
    provider.save()

    added = 0
    for need, price in row.services:
        if provider.services.filter(need=need).exists():
            continue
        Service.objects.create(
            provider=provider, need=need,
            name_en=_capital(NEED_NAMES["en"][need])[:80],
            name_sw=_capital(NEED_NAMES["sw"][need])[:80],
            price_tsh=price or 0,
            # An unknown price is imported switched off, so no one is told
            # "free" by mistake. Staff set the price and switch it on.
            is_active=price is not None,
        )
        added += 1
    return created, added


class Command(BaseCommand):
    help = "Import providers from a CSV file. New providers are always pending verification."

    def add_arguments(self, parser):
        parser.add_argument("csv_file")
        parser.add_argument("--dry-run", action="store_true",
                            help="Check the file and report, without saving anything.")

    def handle(self, *args, **options):
        try:
            with open(options["csv_file"], newline="", encoding="utf-8-sig") as handle:
                rows, errors = parse(handle)
        except FileNotFoundError as problem:
            raise CommandError(f"File not found: {options['csv_file']}") from problem
        if errors:
            for error in errors:
                self.stderr.write(error)
            raise CommandError(f"{len(errors)} problem(s); nothing was imported.")
        if options["dry_run"]:
            unknown = sum(1 for row in rows for _, price in row.services if price is None)
            self.stdout.write(f"OK: {len(rows)} provider(s) would be imported; "
                              f"{unknown} service(s) have no price yet and would be inactive.")
            return
        created = updated = services = 0
        with transaction.atomic():
            for row in rows:
                new, added = save(row)
                created += new
                updated += not new
                services += added
        self.stdout.write(
            f"Imported {created} new and updated {updated} provider(s), added {services} "
            f"service(s). New providers are PENDING: check phone, licence and prices, then "
            f"use 'Mark verified' in the admin.")
