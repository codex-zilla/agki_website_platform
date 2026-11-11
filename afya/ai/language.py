"""Understanding what a girl or woman types, in everyday Swahili or English.

Messages arrive as SMS shorthand, mixed Swahili and English ("nataka family
planning"), with misspellings and stretched letters ("sanaaa"). This module
turns such text into:

* the language to answer in,
* the health need(s) she is describing, with a confidence,
* the region/district she mentions, if any.

It runs entirely on the server with no external AI service, so it works on a
cheap host, costs nothing per message and never sends her words to a third
party. The vocabulary below is the "model"; it is meant to grow as real
messages are reviewed (see `afya/README.md`, "Improving the AI").
"""

import difflib
import re
import unicodedata
from dataclasses import dataclass, field

from afya.models import Language, Need

# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------

#: Common SMS shorthand -> full word.
SHORTHAND = {
    "nna": "nina", "nnaumwa": "ninaumwa", "nataka": "nataka", "ntaka": "nataka",
    "mm": "mimi", "cjui": "sijui", "sjui": "sijui", "xaxa": "sasa", "cku": "siku",
    "ckuz": "siku", "hv": "hivi", "plz": "please", "pls": "please", "u": "you",
    "ur": "your", "im": "i am", "i'm": "i am", "dont": "do not", "cant": "can not",
    "kliniki": "kliniki", "clinic": "kliniki", "hospitali": "hospitali",
    "preg": "pregnant", "pregnent": "pregnant", "pregnat": "pregnant",
    "cannot": "can not", "doesnt": "does not", "isnt": "is not", "wasnt": "was not",
    "didnt": "did not", "havent": "have not",
}


def normalise(text):
    """Lower-case, strip accents and punctuation, expand shorthand."""
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    text = text.lower()
    # Apostrophes join, they don't split: "can't" -> "cant" -> "can not",
    # "won't" -> "wont". Splitting them hid danger signs like "I can't breathe".
    text = re.sub(r"['\u2019`]", "", text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    # "sanaaaa" -> "sanaa": keep at most two of any repeated letter.
    text = re.sub(r"([a-z])\1{2,}", r"\1\1", text)
    words = [SHORTHAND.get(word, word) for word in text.split()]
    return " ".join(words)


# --------------------------------------------------------------------------
# Language
# --------------------------------------------------------------------------

SWAHILI_MARKERS = {
    "nina", "nataka", "naomba", "habari", "mimi", "sana", "na", "ya", "wa", "kwa",
    "ni", "je", "wapi", "gani", "nini", "siku", "mimba", "damu", "tumbo", "kliniki",
    "hospitali", "msaada", "shikamoo", "mambo", "asante", "sijui", "ninaumwa", "nko",
    "niko", "karibu", "huduma", "kupima", "kipimo", "uzazi", "mpango", "ndiyo", "hapana",
}
ENGLISH_MARKERS = {
    "i", "am", "the", "my", "want", "need", "help", "where", "is", "please", "have",
    "pregnant", "test", "clinic", "doctor", "pain", "bleeding", "hello", "hi", "can",
    "what", "how", "yes", "no", "planning", "family", "period", "near", "me",
}


def detect_language(text, default=Language.SWAHILI):
    words = set(normalise(text).split())
    sw = len(words & SWAHILI_MARKERS)
    en = len(words & ENGLISH_MARKERS)
    if en > sw:
        return Language.ENGLISH
    if sw > en:
        return Language.SWAHILI
    return default


# --------------------------------------------------------------------------
# Needs
# --------------------------------------------------------------------------

#: Phrases that point to each need, with a weight. Multi-word phrases score
#: higher because they are less ambiguous ("kipimo cha mimba" vs "kipimo").
NEED_PHRASES = {
    Need.ANTENATAL: [
        "kliniki ya wajawazito", "kliniki ya mama", "kliniki ya ujauzito",
        "mjamzito", "ujauzito", "nina mimba", "mimba ya", "antenatal", "anc",
        "i am pregnant", "pregnant", "pregnancy check", "pregnancy clinic",
        "mama mjamzito", "kujifungua", "delivery", "ultrasound", "utra sound",
    ],
    Need.PREGNANCY_TEST: [
        "kipimo cha mimba", "kupima mimba", "pima mimba", "pregnancy test",
        "nimechelewa siku", "sijaona siku", "sijaingia siku", "period late",
        "missed period", "late period", "am i pregnant", "nina mimba au",
    ],
    Need.FAMILY_PLANNING: [
        "uzazi wa mpango", "family planning", "majira", "vidonge vya majira",
        "sindano ya uzazi", "sindano ya majira", "kitanzi", "kipandikizi",
        "implant", "contraceptive", "contraception", "kondomu", "condom",
        "emergency pill", "p2", "postinor", "morning after", "njiti",
    ],
    Need.STI: [
        "magonjwa ya zinaa", "ugonjwa wa zinaa", "zinaa", "sti", "std",
        "kutokwa uchafu", "uchafu ukeni", "discharge", "kuwashwa sehemu za siri",
        "muwasho", "itching down", "kaswende", "kisonono", "gonorrhea", "syphilis",
        "vidonda sehemu za siri",
    ],
    Need.HIV: [
        "vvu", "ukimwi", "hiv", "aids", "kupima ukimwi", "kupima vvu",
        "prep", "pep", "arv", "dawa za kufubaza",
    ],
    Need.MENSTRUAL: [
        "hedhi", "maumivu ya hedhi", "siku zangu", "period pain", "menstrual",
        "period", "pedi", "pads", "damu ya mwezi", "hedhi nyingi",
    ],
    Need.POSTNATAL: [
        "baada ya kujifungua", "nimejifungua", "postnatal", "after birth",
        "after delivery", "mtoto mchanga", "newborn", "kunyonyesha", "breastfeeding",
        "chanjo ya mtoto",
    ],
    Need.CERVICAL: [
        "saratani ya shingo ya kizazi", "shingo ya kizazi", "cervical",
        "pap smear", "via screening", "cancer screening", "saratani",
    ],
    Need.LAB_TEST: [
        "kipimo", "vipimo", "maabara", "lab test", "laboratory", "malaria",
        "kupima damu", "blood test", "test", "kupima", "uti", "mkojo", "urine",
        "sukari", "diabetes", "presha", "pressure",
    ],
    Need.MENTAL_HEALTH: [
        "msongo wa mawazo", "msongo", "stress", "depression", "huzuni sana",
        "wasiwasi", "anxiety", "sina furaha", "mental health", "counselling",
        "ushauri nasaha",
    ],
    Need.GENERAL: [
        "daktari", "doctor", "ninaumwa", "naumwa", "i am sick", "homa", "fever",
        "maumivu", "pain", "consultation", "kuonana na daktari",
    ],
}

PREGNANCY_TERMS = (
    "mimba", "mjamzito", "ujauzito", "pregnant", "pregnancy", "wajawazito",
    "antenatal", "mama mjamzito",
)

#: Words long enough to be matched despite a typo ("ujauzto" -> "ujauzito").
_FUZZY_MIN_LENGTH = 5
_FUZZY_CUTOFF = 0.84


@dataclass
class Understanding:
    language: str
    needs: list = field(default_factory=list)  # [(need, score)], best first
    region: str = ""
    district: str = ""
    mentions_pregnancy: bool = False
    is_adolescent: bool = False
    text: str = ""

    @property
    def need(self):
        return self.needs[0][0] if self.needs else ""

    @property
    def confidence(self):
        if not self.needs:
            return 0.0
        best = self.needs[0][1]
        runner_up = self.needs[1][1] if len(self.needs) > 1 else 0
        return round(best / (best + runner_up), 2)


def contains(text, phrase):
    """Whole-word containment, tolerant of small spelling mistakes."""
    padded = f" {text} "
    if f" {phrase} " in padded:
        return True
    if " " in phrase or len(phrase) < _FUZZY_MIN_LENGTH:
        return False
    words = [word for word in text.split() if len(word) >= _FUZZY_MIN_LENGTH]
    return bool(difflib.get_close_matches(phrase, words, n=1, cutoff=_FUZZY_CUTOFF))


def score_needs(text):
    scores = {}
    for need, phrases in NEED_PHRASES.items():
        total = 0.0
        for phrase in phrases:
            if contains(text, phrase):
                total += 1.0 + 0.75 * phrase.count(" ")
        if total:
            scores[need] = total
    # A pregnancy test is about not knowing yet; don't let "mimba" in
    # "kipimo cha mimba" turn it into antenatal care.
    if Need.PREGNANCY_TEST in scores and Need.ANTENATAL in scores:
        scores[Need.ANTENATAL] -= 1.0
    # LAB_TEST and GENERAL are catch-alls: only win when nothing specific did.
    specific = {n: s for n, s in scores.items() if n not in (Need.LAB_TEST, Need.GENERAL) and s > 0}
    if specific:
        for catch_all in (Need.LAB_TEST, Need.GENERAL):
            if catch_all in scores:
                scores[catch_all] *= 0.4
    return sorted(((n, s) for n, s in scores.items() if s > 0), key=lambda item: -item[1])


# --------------------------------------------------------------------------
# Places
# --------------------------------------------------------------------------

#: Region -> districts / main towns. Pilot regions are listed in full; the
#: rest by region name so a mention is still recognised.
PLACES = {
    "Dar es Salaam": ["Ilala", "Kinondoni", "Temeke", "Ubungo", "Kigamboni"],
    "Pwani": ["Kibaha", "Bagamoyo", "Chalinze", "Mkuranga", "Kisarawe", "Rufiji",
              "Kibiti", "Mafia"],
    "Morogoro": ["Morogoro Mjini", "Mvomero", "Kilosa", "Kilombero", "Ifakara",
                 "Ulanga", "Gairo", "Malinyi", "Mtibwa", "Turiani"],
    "Dodoma": ["Dodoma Mjini", "Chamwino", "Kondoa", "Mpwapwa", "Bahi", "Kongwa",
               "Chemba"],
    "Mwanza": ["Nyamagana", "Ilemela", "Magu", "Sengerema", "Kwimba", "Misungwi",
               "Ukerewe", "Buchosa"],
    "Mbeya": ["Mbeya Mjini", "Rungwe", "Tukuyu", "Kyela", "Chunya", "Mbarali",
              "Mbalizi"],
    "Tabora": ["Tabora Mjini", "Nzega", "Igunga", "Urambo", "Uyui", "Sikonge",
               "Kaliua"],
    "Arusha": [], "Kilimanjaro": [], "Tanga": [], "Iringa": [], "Njombe": [],
    "Ruvuma": [], "Mtwara": [], "Lindi": [], "Singida": [], "Shinyanga": [],
    "Simiyu": [], "Mara": [], "Kagera": [], "Geita": [], "Kigoma": [], "Katavi": [],
    "Rukwa": [], "Songwe": [], "Manyara": [],
}

#: Towns that sit inside a district under another name.
TOWN_TO_DISTRICT = {"Mtibwa": "Mvomero", "Turiani": "Mvomero", "Ifakara": "Kilombero",
                    "Tukuyu": "Rungwe", "Mbalizi": "Mbeya Mjini", "Chalinze": "Bagamoyo"}

ALIASES = {"dar": "Dar es Salaam", "dsm": "Dar es Salaam", "coast": "Pwani",
           "morogoro town": "Morogoro Mjini", "dodoma town": "Dodoma Mjini"}


def find_place(text):
    """Return (region, district) mentioned in normalised `text`, or blanks."""
    for alias, place in ALIASES.items():
        if contains(text, alias):
            text = f"{text} {normalise(place)}"
    for region, districts in PLACES.items():
        for district in districts:
            if contains(text, normalise(district)):
                return region, TOWN_TO_DISTRICT.get(district, district)
    for region in PLACES:
        if contains(text, normalise(region)):
            return region, ""
    return "", ""


#: Phrases that say she is NOT pregnant, or doesn't know yet. They still count
#: as pregnancy words for danger signs (safer to over-warn), but they must not
#: mark her as pregnant and start antenatal reminders on a possibly shared phone.
NOT_PREGNANT = ("sina mimba", "si mjamzito", "sio mjamzito", "not pregnant",
                "am i pregnant", "nina mimba au", "kama nina mimba", "if i am pregnant")


def says_pregnant(clean, needs):
    """True only when she says she is pregnant, not when she asks for a test."""
    if needs and needs[0][0] == Need.PREGNANCY_TEST:
        return False
    if any(contains(clean, phrase) for phrase in NOT_PREGNANT):
        return False
    return any(contains(clean, term) for term in PREGNANCY_TERMS)


ADOLESCENT_PATTERN = re.compile(r"\b(1[0-9])\s*(years?|yrs?|miaka)\b|\bmiaka\s*(1[0-9])\b")
ADOLESCENT_WORDS = ("mwanafunzi", "student", "shule", "school", "msichana", "teen", "kijana")


def understand(text, default_language=Language.SWAHILI):
    clean = normalise(text)
    region, district = find_place(clean)
    needs = score_needs(clean)
    return Understanding(
        language=detect_language(text, default_language),
        needs=needs,
        region=region,
        district=district,
        mentions_pregnancy=says_pregnant(clean, needs),
        is_adolescent=bool(ADOLESCENT_PATTERN.search(clean))
        or any(contains(clean, word) for word in ADOLESCENT_WORDS),
        text=clean,
    )
