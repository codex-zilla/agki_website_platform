import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import CommandError, call_command
from django.test import TestCase

from afya.ai import ranking
from afya.models import Need, Provider, Service

DATA = Path(__file__).resolve().parent.parent / "data"


class ImportProvidersTests(TestCase):
    def run_import(self, path, *args):
        out = StringIO()
        call_command("afya_import_providers", str(path), *args, stdout=out, stderr=StringIO())
        return out.getvalue()

    def test_real_public_list_loads_as_pending(self):
        self.run_import(DATA / "providers_tz_public.csv")
        self.assertEqual(Provider.objects.count(), 9)
        self.assertFalse(Provider.objects.exclude(
            verification=Provider.Verification.PENDING).exists())
        tumbi = Provider.objects.get(name="Tumbi Regional Referral Hospital")
        self.assertEqual((tumbi.region, tumbi.district, tumbi.kind),
                         ("Pwani", "Kibaha", Provider.Kind.PUBLIC))
        # Free by policy for pregnant women: active and free.
        anc = tumbi.services.get(need=Need.ANTENATAL)
        self.assertEqual((anc.price_tsh, anc.is_active), (0, True))
        # Unknown price: imported switched off.
        self.assertFalse(tumbi.services.get(need=Need.FAMILY_PLANNING).is_active)
        self.assertEqual(anc.name_sw, "Kliniki ya ujauzito")

    def test_pending_providers_are_never_matched(self):
        self.run_import(DATA / "providers_tz_public.csv")
        self.assertEqual(ranking.rank(Need.ANTENATAL, "Pwani", "Kibaha"), [])
        self.assertEqual(ranking.emergency_providers("Pwani", "Kibaha"), [])

    def test_reimport_keeps_staff_edits_and_verification(self):
        path = DATA / "providers_tz_public.csv"
        self.run_import(path)
        tumbi = Provider.objects.get(name="Tumbi Regional Referral Hospital")
        tumbi.phone, tumbi.licence_number = "0232402000", "HFR-123"
        tumbi.verification = Provider.Verification.VERIFIED
        tumbi.save()
        fp = tumbi.services.get(need=Need.FAMILY_PLANNING)
        fp.price_tsh, fp.is_active = 0, True
        fp.save()
        self.run_import(path)
        tumbi.refresh_from_db()
        fp.refresh_from_db()
        self.assertEqual((tumbi.phone, tumbi.licence_number, tumbi.verification),
                         ("0232402000", "HFR-123", Provider.Verification.VERIFIED))
        self.assertTrue(fp.is_active)
        self.assertEqual(Provider.objects.count(), 9)

    def test_dry_run_saves_nothing(self):
        out = self.run_import(DATA / "providers_tz_public.csv", "--dry-run")
        self.assertIn("9 provider(s)", out)
        self.assertFalse(Provider.objects.exists())

    def test_bad_rows_stop_the_whole_import(self):
        bad = Path(tempfile.mkdtemp()) / "bad.csv"
        bad.write_text(
            "name,kind,region,district,council,services\n"
            "Good Clinic,clinic,Pwani,Kibaha,mct,family_planning:0\n"
            "Bad Clinic,clinic,Pwani,Nowhere,mct,family_planning:0\n"
            "Worse Clinic,shop,Pwani,Kibaha,mct,dentist:100\n")
        try:
            with self.assertRaises(CommandError):
                self.run_import(bad)
        finally:
            bad.unlink()
        self.assertFalse(Provider.objects.exists())
        self.assertFalse(Service.objects.exists())

    def test_template_has_every_column_and_no_rows(self):
        header = (DATA / "providers_template.csv").read_text().splitlines()
        self.assertEqual(len(header), 1)
        self.assertIn("services", header[0])

    def test_partner_clinic_row_imports(self):
        path = Path(tempfile.mkdtemp()) / "partners.csv"
        path.write_text(
            (DATA / "providers_template.csv").read_text()
            + 'AGKI Partner Clinic,clinic,Pwani,Kibaha,"Mailimoja, Kibaha",0754000000,'
              '0754000000,MCT/1,mct,Mon-Sat 8-18,yes,no,'
              "family_planning:0;pregnancy_test:2000,AGKI visit,\n")
        self.run_import(path)
        clinic = Provider.objects.get()
        self.assertTrue(clinic.is_youth_friendly)
        self.assertEqual(clinic.services.get(need=Need.PREGNANCY_TEST).price_tsh, 2000)
