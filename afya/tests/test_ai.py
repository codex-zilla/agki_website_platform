from django.test import SimpleTestCase, TestCase, override_settings

from afya.ai import language, ranking, safety
from afya.models import Need
from afya.tests.helpers import provider


class UnderstandingTests(SimpleTestCase):
    def need(self, text):
        return language.understand(text).need

    def test_everyday_swahili_and_english(self):
        cases = {
            "nataka uzazi wa mpango": Need.FAMILY_PLANNING,
            "nataka vidonge vya majira": Need.FAMILY_PLANNING,
            "I need family planning": Need.FAMILY_PLANNING,
            "nimechelewa siku nataka kipimo cha mimba": Need.PREGNANCY_TEST,
            "am i pregnant? my period is late": Need.PREGNANCY_TEST,
            "nina mimba nataka kliniki": Need.ANTENATAL,
            "I am pregnant, where is a clinic": Need.ANTENATAL,
            "nataka kupima VVU": Need.HIV,
            "natokwa na uchafu ukeni": Need.STI,
            "maumivu ya hedhi kila mwezi": Need.MENSTRUAL,
            "nina msongo wa mawazo": Need.MENTAL_HEALTH,
            "nataka kipimo cha malaria": Need.LAB_TEST,
        }
        for text, need in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.need(text), need)

    def test_sms_shorthand_and_typos(self):
        self.assertEqual(self.need("NNA UJAUZTO NATAKA KLINIKI"), Need.ANTENATAL)
        self.assertEqual(self.need("im pregnent"), Need.ANTENATAL)

    def test_language_detection(self):
        self.assertEqual(language.detect_language("nataka msaada sana"), "sw")
        self.assertEqual(language.detect_language("I need help please"), "en")

    def test_places_and_towns(self):
        self.assertEqual(language.find_place(language.normalise("niko kibaha")), ("Pwani", "Kibaha"))
        self.assertEqual(language.find_place(language.normalise("nipo Mtibwa")), ("Morogoro", "Mvomero"))
        self.assertEqual(language.find_place(language.normalise("I live in dar")),
                         ("Dar es Salaam", ""))
        self.assertEqual(language.find_place(language.normalise("Mwanza")), ("Mwanza", ""))

    def test_adolescent(self):
        self.assertTrue(language.understand("nina miaka 16 nataka ushauri").is_adolescent)
        self.assertFalse(language.understand("nina miaka 26").is_adolescent)


class SafetyTests(SimpleTestCase):
    def test_danger_signs_are_caught(self):
        cases = {
            "Ninatokwa na damu, nina mimba": "bleeding_pregnancy",
            "I am pregnant and bleeding": "bleeding_pregnancy",
            "nimejifungua jana natoka damu nyingi": "bleeding_pregnancy",
            "mtoto hachezi tumboni": "baby_not_moving",
            "maji yametoka": "waters_broke",
            "mjamzito kichwa kinauma sana naona giza": "headache_vision",
            "she is having convulsions": "convulsions",
            "nimebakwa jana": "sexual_violence",
            "nataka kujiua": "self_harm",
        }
        for text, code in cases.items():
            with self.subTest(text=text):
                result = safety.check(text)
                self.assertIsNotNone(result)
                self.assertEqual(result.code, code)

    def test_ordinary_requests_are_not_alarms(self):
        for text in ("nataka uzazi wa mpango", "kipimo cha mimba", "period pain",
                     "nataka kupima damu", "headache"):
            with self.subTest(text=text):
                self.assertIsNone(safety.check(text))

    def test_known_pregnancy_counts(self):
        self.assertIsNone(safety.check("nina homa"))
        self.assertEqual(safety.check("nina homa", known_pregnant=True).code, "fever_pregnancy")
        self.assertEqual(safety.check("natoka damu", known_pregnant=True).code,
                         "bleeding_pregnancy")

    @override_settings(AFYA_EMERGENCY_NUMBER="112")
    def test_message_sends_to_emergency_care_and_never_diagnoses(self):
        result = safety.check("I am pregnant and bleeding")
        for lang in ("sw", "en"):
            message = result.message(lang)
            self.assertIn("112", message)
            self.assertNotIn("you have", message.lower())

    def test_every_rule_speaks_both_languages(self):
        for rule in safety.RULES:
            self.assertTrue(rule.message_sw and rule.message_en, rule.code)


class RankingTests(TestCase):
    def test_only_verified_providers_offering_the_service(self):
        provider("Verified", services=((Need.HIV, 0),))
        provider("Unverified", verified=False, services=((Need.HIV, 0),))
        provider("Other service", services=((Need.STI, 0),))
        names = [m.provider.name for m in ranking.rank(Need.HIV, "Pwani", "Kibaha")]
        self.assertEqual(names, ["Verified"])

    def test_nearer_and_cheaper_rank_higher(self):
        provider("Far", district="Bagamoyo", services=((Need.HIV, 5000),))
        provider("Near costly", services=((Need.HIV, 5000),))
        provider("Near free", services=((Need.HIV, 0),))
        names = [m.provider.name for m in ranking.rank(Need.HIV, "Pwani", "Kibaha")]
        self.assertEqual(names, ["Near free", "Near costly", "Far"])

    def test_free_next_district_can_beat_expensive_nearby(self):
        # A deliberate trade-off: TSh 40,000 is a real barrier, a short trip less so.
        provider("Far free", district="Bagamoyo", services=((Need.HIV, 0),))
        provider("Near expensive", services=((Need.HIV, 40000),))
        names = [m.provider.name for m in ranking.rank(Need.HIV, "Pwani", "Kibaha")]
        self.assertEqual(names[0], "Far free")

    def test_youth_friendly_wins_for_adolescents(self):
        provider("Adult", services=((Need.FAMILY_PLANNING, 0),))
        provider("Youth", is_youth_friendly=True, services=((Need.FAMILY_PLANNING, 0),))
        top = ranking.rank(Need.FAMILY_PLANNING, "Pwani", "Kibaha", adolescent=True)[0]
        self.assertEqual(top.provider.name, "Youth")
        self.assertIn("youth_friendly", top.reasons)

    def test_voucher_makes_a_paid_service_competitive(self):
        provider("Paid", services=((Need.STI, 10000),))
        match = ranking.rank(Need.STI, "Pwani", "Kibaha", voucher_value=10000)[0]
        self.assertAlmostEqual(ranking.cost_score(10000, 10000), 1.0)
        self.assertEqual(match.provider.name, "Paid")

    def test_looks_beyond_region_only_when_nothing_local(self):
        provider("Dar clinic", region="Dar es Salaam", district="Ilala", services=((Need.CERVICAL, 0),))
        self.assertEqual(len(ranking.rank(Need.CERVICAL, "Pwani", "Kibaha")), 1)

    def test_gps_distance(self):
        provider("GPS", latitude=-6.77, longitude=38.92, services=((Need.HIV, 0),))
        match = ranking.rank(Need.HIV, location=(-6.80, 38.95))[0]
        self.assertLess(match.distance_km, 10)
