"""Danger-sign rules. Checked before anything else, on every message.

The AI guides, it never diagnoses. When a message describes a sign that can
mean a life-threatening problem, the only answer is: go to emergency care now.
No booking flow, no questions first.

These rules are written as data so a clinician can review them line by line.
`RULES_VERSION` and `CLINICAL_REVIEW` must be updated whenever a rule changes,
and the change signed off by AGKI's medical lead before release.
"""

from dataclasses import dataclass

from django.conf import settings

from afya.ai.language import PREGNANCY_TERMS, contains, normalise

RULES_VERSION = "2026.11-draft.2"

#: Filled in by AGKI's medical lead when they sign off this version, and kept
#: in the code so the sign-off travels with the rules it covers. Until both
#: are set, the admin shows a warning and `manage.py check --deploy` warns.
#:   reviewed_by: full name and professional registration number (MCT/TNMC)
#:   reviewed_on: date of sign-off, YYYY-MM-DD
CLINICAL_REVIEW = {
    "reviewed_by": "",
    "reviewed_on": "",
    "based_on": "WHO and Tanzania MoH antenatal danger signs",
}

POSTNATAL_TERMS = ("nimejifungua", "baada ya kujifungua", "after birth", "after delivery",
                   "postnatal", "gave birth")


@dataclass(frozen=True)
class DangerRule:
    code: str
    any_of: tuple            # at least one of these phrases...
    with_any: tuple = ()     # ...and, if given, at least one of these
    pregnancy_only: bool = False
    message_sw: str = ""
    message_en: str = ""


GO_NOW_SW = "Nenda kituo cha afya au hospitali iliyo karibu SASA, au piga {emergency}."
GO_NOW_EN = "Go to the nearest health facility or hospital NOW, or call {emergency}."

BLEEDING = ("natoka damu", "ninatoka damu", "ninatokwa na damu", "natokwa na damu",
            "kutokwa damu", "kutokwa na damu", "damu nyingi", "bleeding", "bleed",
            "blood coming")

RULES = (
    DangerRule(
        code="bleeding_pregnancy",
        any_of=BLEEDING,
        with_any=PREGNANCY_TERMS + POSTNATAL_TERMS,
        message_sw="Kutokwa damu ukiwa mjamzito au baada ya kujifungua ni dalili ya hatari.",
        message_en="Bleeding in pregnancy or after giving birth is a danger sign.",
    ),
    DangerRule(
        code="heavy_bleeding",
        any_of=("damu nyingi sana", "heavy bleeding", "bleeding a lot", "damu haikati",
                "damu haiachi", "bleeding wont stop", "bleeding will not stop",
                "bleeding heavily"),
        message_sw="Kutokwa damu nyingi ni dalili ya hatari.",
        message_en="Heavy bleeding is a danger sign.",
    ),
    DangerRule(
        code="convulsions",
        any_of=("degedege", "kifafa", "kuzimia", "amezimia", "nimezimia", "fits",
                "convulsion", "convulsions", "seizure", "fainted", "unconscious",
                "anatetemeka"),
        message_sw="Degedege au kuzimia ni dalili ya hatari.",
        message_en="Fits or fainting are danger signs.",
    ),
    DangerRule(
        code="headache_vision",
        any_of=("kichwa kinauma sana", "maumivu makali ya kichwa", "severe headache",
                "bad headache", "kuona giza", "naona giza", "blurred vision",
                "can not see well", "macho yanaona ukungu"),
        pregnancy_only=True,
        message_sw="Kichwa kuuma sana au kuona giza ukiwa mjamzito ni dalili ya hatari.",
        message_en="A severe headache or blurred vision in pregnancy is a danger sign.",
    ),
    DangerRule(
        code="waters_broke",
        any_of=("maji yametoka", "maji yanatoka", "chupa imepasuka", "chupa kupasuka",
                "waters broke", "water broke", "waters breaking"),
        message_sw="Maji kutoka ukeni ukiwa mjamzito yanahitaji huduma haraka.",
        message_en="Waters breaking needs care straight away.",
    ),
    DangerRule(
        code="baby_not_moving",
        any_of=("mtoto hachezi", "mtoto hacheza", "mtoto hachezi tumboni",
                "baby not moving", "baby is not moving", "baby stopped moving",
                "no movement"),
        message_sw="Mtoto kuacha kucheza tumboni ni dalili ya hatari.",
        message_en="Your baby moving less or not at all is a danger sign.",
    ),
    DangerRule(
        code="severe_abdominal_pain",
        any_of=("tumbo linauma sana", "maumivu makali ya tumbo", "severe abdominal pain",
                "severe stomach pain", "severe belly pain", "tumbo kuuma sana"),
        message_sw="Maumivu makali ya tumbo ni dalili ya hatari.",
        message_en="Severe abdominal pain is a danger sign.",
    ),
    DangerRule(
        code="fever_pregnancy",
        any_of=("homa kali", "high fever", "fever", "homa"),
        pregnancy_only=True,
        message_sw="Homa ukiwa mjamzito inahitaji kuonwa na mtoa huduma haraka.",
        message_en="Fever in pregnancy needs to be seen quickly.",
    ),
    DangerRule(
        code="breathing",
        any_of=("kushindwa kupumua", "siwezi kupumua", "napumua kwa shida",
                "can not breathe", "difficulty breathing", "short of breath",
                "hard to breathe", "trouble breathing"),
        message_sw="Kushindwa kupumua ni dalili ya hatari.",
        message_en="Difficulty breathing is a danger sign.",
    ),
    DangerRule(
        code="sexual_violence",
        any_of=("nimebakwa", "amenibaka", "kubakwa", "raped", "sexual assault",
                "forced me to have sex", "alinilazimisha"),
        message_sw=("Pole sana. Hili si kosa lako. Nenda kituo cha afya haraka: dawa za "
                    "kuzuia VVU (PEP) na mimba hufanya kazi vizuri ndani ya saa 72."),
        message_en=("We are so sorry. This is not your fault. Go to a health facility "
                    "quickly: medicine to prevent HIV (PEP) and pregnancy works best "
                    "within 72 hours."),
    ),
    DangerRule(
        code="self_harm",
        any_of=("kujiua", "nataka kufa", "kujidhuru", "kujiumiza", "suicide",
                "kill myself", "end my life", "hurt myself", "want to die"),
        message_sw=("Pole sana kwa unachopitia. Hauko peke yako. Tafadhali ongea na mtu "
                    "unayemwamini sasa hivi."),
        message_en=("We are so sorry you are going through this. You are not alone. "
                    "Please talk to someone you trust right now."),
    ),
)


@dataclass
class DangerResult:
    rule: DangerRule

    @property
    def code(self):
        return self.rule.code

    def message(self, language):
        emergency = getattr(settings, "AFYA_EMERGENCY_NUMBER", "112")
        if language == "en":
            return f"{self.rule.message_en} {GO_NOW_EN.format(emergency=emergency)}"
        return f"{self.rule.message_sw} {GO_NOW_SW.format(emergency=emergency)}"


def _matches(rule, text, pregnant):
    if rule.pregnancy_only and not pregnant:
        return False
    if not any(contains(text, phrase) for phrase in rule.any_of):
        return False
    if rule.with_any and not any(contains(text, phrase) for phrase in rule.with_any):
        # Bleeding with no pregnancy words still counts if we know she is pregnant.
        return pregnant and rule.code == "bleeding_pregnancy"
    return True


def check(text, known_pregnant=False):
    """Return the first matching `DangerResult`, or None."""
    clean = normalise(text)
    pregnant = known_pregnant or any(contains(clean, term) for term in PREGNANCY_TERMS)
    for rule in RULES:
        if _matches(rule, clean, pregnant):
            return DangerResult(rule)
    return None


def is_reviewed():
    return bool(CLINICAL_REVIEW["reviewed_by"] and CLINICAL_REVIEW["reviewed_on"])
