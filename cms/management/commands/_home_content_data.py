"""The content that is hardcoded in ``app/templates/app/home.html`` today,
transcribed once so ``seed_home_content`` can load it into the cms models.

Kept in its own module purely so the *copy* stays readable and diffable —
``seed_home_content.py`` is then just the machinery that writes it. If AGKI
changes the live wording before Phase 6 ships, this is the one file to update.

Transcription rules followed here, so a future reader can tell intent from typo:

* **HTML entities are decoded to the real character** — the template's
  ``Gra&ccedil;a`` is stored as ``Graça``, ``&amp;`` as ``&``, and
  ``Co&#8209;founder`` as ``Co‑founder``. These fields render as plain text
  through the template engine's autoescaping, so storing the entity would
  display the entity source literally.
* **Non-breaking hyphens (U+2011) are preserved verbatim.** The live copy uses
  them throughout ("Last‑mile", "e‑prescriptions", "Pay‑per‑service") so those
  terms never wrap mid-word. They look identical to a plain hyphen in most
  editors — do not "clean them up" without checking the rendered result.
* **Whitespace is collapsed the way a browser collapses it.** Source copy split
  across several indented template lines becomes one space-separated string.
* **Rich-text fields carry HTML** (``story_copy``, FAQ ``answer``) because they
  are ``CKEditor5Field``s; plain ``TextField``s carry plain text.
"""

# --- Assets -----------------------------------------------------------------
# Source specs are "<store>:<path within that store>", resolved by the command:
#   static:  looked up through Django's staticfiles finders
#   media:   relative to MEDIA_ROOT (the problem-section images already live
#            there — they were uploaded rather than committed as static files)

SITE = {
    "site_name": "AGKI Medical Lab",
    "tagline": "AI powered · Solar powered",
    "logo": ("static:app/logo.png", "cms/site/logo.png"),
    # The template wires the same logo.png up as the favicon
    # (`<link rel="shortcut icon" href="{% static 'app/logo.png' %}">`),
    # so both fields intentionally point at one copied file.
    "favicon": ("static:app/logo.png", "cms/site/logo.png"),
    "phone": "+255 717 308 796",
    "email": "info@agkimedicallaboratory.co.tz",
    "address_line": "Kibaha, Pwani",
    # The footer's four social icons are bare <i> tags with no href at all —
    # there is no live URL to import. Left blank rather than invented; AGKI can
    # fill them in from the admin and Phase 6 should only render an icon when
    # its URL field is non-empty.
    "facebook_url": "",
    "twitter_url": "",
    "instagram_url": "",
    "linkedin_url": "",
    "google_analytics_id": "G-T256LX3B9J",
    "map_embed_url": (
        "https://www.google.com/maps/embed?pb=!1m18!1m12!1m3!1d3853.555615469414"
        "!2d38.856572674894515!3d-6.7834211663387824!2m3!1f0!2f0!3f0!3m2!1i1024"
        "!2i768!4f13.1!3m3!1m2!1s0x185c6f00e72ce859%3A0xac0ecd8f3b034606"
        "!2sAgki%20medical%20laboratory!5e1!3m2!1sen!2stz"
    ),
    "footer_copyright_text": "© 2026 AGKI Medical Laboratory – transforming rural healthcare",
}

HERO = {
    "kicker_text": "Afya Mfukoni",
    "headline_line1": "Healthcare That",
    "headline_line2": "Reaches Every",
    "headline_accent": "Community",
    "lede": (
        "AGKI combines AI-powered diagnostics, mobile laboratories, telemedicine "
        "and sustainable technology to bring quality healthcare closer to every family."
    ),
    "primary_cta_text": "Book appointment",
    # primary_cta_url is resolved at seed time via reverse("appointment_home")
    # rather than hardcoded here — see the command.
    "secondary_cta_text": "Shop Now",
    "secondary_cta_url": "/ecommerce/home",
}

# `number` holds the machine value the counter animation reads
# (`data-count-to`), not the formatted display string: the template's JS does
# parseInt() on it, and parseInt("500,000") returns 500. Formatting the comma
# back in for display is Phase 6's job (Django's `intcomma` filter).
HERO_IMPACT_STATS = [
    {"icon_class": "fa-solid fa-users", "number": "500000", "suffix": "+", "label": "Patients served"},
    {"icon_class": "fa-solid fa-location-dot", "number": "75", "suffix": "+", "label": "Communities benefited"},
    {"icon_class": "fa-solid fa-headset", "number": "24/7", "suffix": "", "label": "Digital healthcare access"},
]

ABOUT = {
    "story_badge": "Est. 2022",
    "story_heading": "Bridging the Healthcare Gap with Innovation",
    "story_copy": (
        "<p>AGKI delivers integrated healthcare through solar-powered mobile clinics "
        "and an AI-powered digital health platform that connects patients with doctors, "
        "laboratories, pharmacies, and hospitals for appointments, diagnostics, "
        "telemedicine, digital prescriptions, and medication access. To date, we have "
        "helped over 500,000 people access quality healthcare services. Our diversified "
        "revenue model including pay-per-service, subscriptions, and strategic "
        "partnerships ensures long-term sustainability and scalable impact.</p>"
    ),
    "mission_text": (
        "To provide accurate, affordable, and timely diagnostic and healthcare services "
        "that improve health outcomes for everyone, everywhere."
    ),
    "vision_text": (
        "To revolutionize healthcare delivery in Tanzania and beyond by making quality, "
        "accessible, and affordable healthcare available to everyone through innovative, "
        "technology-driven, and environmentally sustainable solutions."
    ),
    "youtube_embed_url": "https://www.youtube.com/embed/cpnHYjFKyjs?si=FTEfPQryNycoIDjq",
}

CORE_VALUES = [
    {"icon_class": "fas fa-heart", "label": "Compassion"},
    {"icon_class": "fas fa-flask", "label": "Innovation"},
    {"icon_class": "fas fa-hand-holding-heart", "label": "Integrity"},
    {"icon_class": "fas fa-leaf", "label": "Sustainability"},
]

PROBLEM_SECTION = {
    "eyebrow": "A Silent Emergency in Tanzania",
    "heading": "The Healthcare Gap in Sub-Saharan Countries",
    "lede": (
        "Every day, thousands face impossible choices between health and survival "
        "simply because healthcare is too far away."
    ),
    "cta_text": "Explore AGKI's Solution",
    "cta_url": "#services",
}

# `body` is a plain TextField (not rich text), so the two source paragraphs are
# stored separated by a blank line. Phase 6 should render it through Django's
# `linebreaks` filter to get the two <p> tags back.
PROBLEM_CARDS = [
    {
        "asset": ("media:problem/problem_1.jpg", "cms/problem/problem_1.jpg"),
        "image_alt": "An overcrowded hospital ward with limited space and resources",
        "icon_class": "fas fa-hospital-alt",
        "title": "Inadequate Infrastructure and Supply Chains",
        "body": (
            "Many clinics and hospitals, particularly in rural areas, operate without "
            "beds, reliable electricity, running water, or basic surgical facilities.\n\n"
            "Weak supply chains compound the challenge, causing chronic shortages of "
            "essential medications, vaccines, and diagnostic equipment."
        ),
    },
    {
        "asset": ("media:problem/problem_2.jpg", "cms/problem/problem_2.jpg"),
        "image_alt": "A healthcare worker standing inside a medical facility",
        "icon_class": "fas fa-user-md",
        "title": "Critical Shortage of Healthcare Workers",
        "body": (
            "Sub-Saharan Africa bears over 20% of the global disease burden but houses "
            "only a tiny fraction of the world's healthcare workforce.\n\n"
            "Limited training capacity and “brain drain” continue to deepen shortages "
            "of doctors, nurses, and midwives across the region."
        ),
    },
    {
        "asset": ("media:problem/problem_3.jpg", "cms/problem/problem_3.jpg"),
        "image_alt": "A patient holding money beside medicine and a medical bill",
        "icon_class": "fas fa-hand-holding-usd",
        "title": "High Out-of-Pocket Costs & Lack of Funding",
        "body": (
            "Most countries in the region lack comprehensive health insurance systems, "
            "leaving patients to pay directly for medical care.\n\n"
            "The resulting financial hardship forces many families to delay or "
            "completely skip lifesaving treatment."
        ),
    },
]

PROBLEM_STATS = [
    {
        "icon_class": "fas fa-route",
        "number": "75%",
        "label": "Limited access to healthcare",
        "description": "Many people still travel long distances to reach essential medical services.",
    },
    {
        "icon_class": "fas fa-shield-heart",
        "number": "60%+",
        "label": "Preventable diseases",
        "description": "Can be managed through early detection and timely treatment.",
    },
    {
        "icon_class": "fas fa-people-group",
        "number": "Millions",
        "label": "Need better healthcare access",
        "description": "Digital health solutions are transforming how care is delivered across Tanzania.",
    },
]

SOLUTIONS = [
    {
        "icon_class": "fas fa-solar-panel",
        "title": "Mobile Clinics (solar bikes & vans)",
        "description": "Diagnostics, essential meds, health education at low cost.",
    },
    {
        "icon_class": "fas fa-robot",
        "title": "AI health platform (WhatsApp)",
        "description": "Instant results, scheduling, e‑prescriptions, even offline.",
    },
    {
        "icon_class": "fas fa-user-md",
        "title": "Specialist tele‑consultations",
        "description": "Connect with doctors without travelling.",
    },
    {
        "icon_class": "fas fa-baby",
        "title": "Maternal & child health",
        "description": "Antenatal, nutrition, immunisation outreach.",
    },
    {
        "icon_class": "fas fa-leaf",
        "title": "Solar‑electric & cost‑effective",
        "description": "Lower footprint, affordable services.",
    },
    {
        "icon_class": "fas fa-handshake",
        "title": "Diversified business model",
        "description": "Pay‑per‑service + subscriptions + NGO partnerships.",
    },
]

WHY_US = [
    {"icon_class": "fas fa-map-marker-alt", "title": "Last‑mile reach", "description": "Healthcare at your doorstep."},
    {
        "icon_class": "fas fa-hand-holding-usd",
        "title": "Affordable services",
        "description": "Pricing for low‑income families.",
    },
    {"icon_class": "fas fa-microchip", "title": "Cutting‑edge AI", "description": "Enhanced diagnostics & management."},
    {
        "icon_class": "fas fa-users",
        "title": "Community empowerment",
        "description": "Partner with local health workers.",
    },
    {"icon_class": "fas fa-globe-africa", "title": "Eco‑friendly ops", "description": "Solar, electric, sustainable."},
    {"icon_class": "fas fa-heartbeat", "title": "Comprehensive care", "description": "End‑to‑end health services."},
]

AWARD_LOGOS = [
    {"asset": ("static:app/sankalplogo.png", "cms/awards/sankalplogo.png"), "alt_text": "Sankalp", "size": "normal"},
    {"asset": ("static:app/undp.png", "cms/awards/undp.png"), "alt_text": "UNDP", "size": "normal"},
    {"asset": ("static:app/unfpa.png", "cms/awards/unfpa.png"), "alt_text": "UNFPA", "size": "normal"},
    {"asset": ("static:app/villgro-logo.png", "cms/awards/villgro-logo.png"), "alt_text": "Villgro", "size": "lg"},
    {"asset": ("static:app/w4g.png", "cms/awards/w4g.png"), "alt_text": "Women for Girls", "size": "normal"},
    {"asset": ("static:app/AFD.png", "cms/awards/AFD.png"), "alt_text": "AFD", "size": "xl"},
    {"asset": ("static:app/graca.png", "cms/awards/graca.png"), "alt_text": "Graça Machel Trust", "size": "normal"},
    {"asset": ("static:app/afrishela.jpeg", "cms/awards/afrishela.jpeg"), "alt_text": "Afrishela", "size": "normal"},
]

# The two live testimonials are bare <video> tags with no caption or
# attribution anywhere in the template, so `patient_name` is genuinely empty —
# `Testimonial.clean()` is satisfied by `video_file` alone.
TESTIMONIALS = [
    {"asset": ("static:app/mama.mp4", "cms/testimonials/mama.mp4"), "patient_name": ""},
    {"asset": ("static:app/testmony2.mp4", "cms/testimonials/testmony2.mp4"), "patient_name": ""},
]

COMMUNITY_PHOTOS = [
    {"asset": ("static:app/1.jpg", "cms/community/1.jpg"), "caption": "Mobile clinic outreach"},
    {"asset": ("static:app/2.jpg", "cms/community/2.jpg"), "caption": "Free health screening day"},
    {"asset": ("static:app/3.jpg", "cms/community/3.jpg"), "caption": "Maternal health workshop"},
    {"asset": ("static:app/4.jpg", "cms/community/4.jpg"), "caption": "Youth wellness drive"},
    {"asset": ("static:app/5.jpg", "cms/community/5.jpg"), "caption": "Community health fair"},
    {"asset": ("static:app/6.jpg", "cms/community/6.jpg"), "caption": "Volunteer appreciation day"},
]

# `body` is left empty on purpose: the live cards have no article behind them
# (their "Read more" links are href="#"), so there is no real body copy to
# import. The field exists for the future full-article page in the design doc.
NEWS_POSTS = [
    {
        "asset": ("static:app/maternity.jpg", "cms/news/2025/06/maternity.jpg"),
        "title": "Maternal health launch",
        "slug": "maternal-health-launch",
        "published_date": "2025-06-15",
        "excerpt": (
            "AGKI Laboratory introduces comprehensive maternal health services to "
            "support expecting mothers in our community."
        ),
    },
    {
        "asset": ("static:app/community.png", "cms/news/2025/06/community.png"),
        "title": "Community fair success",
        "slug": "community-fair-success",
        "published_date": "2025-06-10",
        "excerpt": (
            "Over 500 community members benefited from our free health screening and "
            "awareness programs last month."
        ),
    },
    {
        "asset": ("static:app/partnership.jpeg", "cms/news/2025/06/partnership.jpeg"),
        "title": "Hospital partnership",
        "slug": "hospital-partnership",
        "published_date": "2025-06-05",
        "excerpt": (
            "New collaboration enhances our referral system and ensures continuity of "
            "care for complex cases."
        ),
    },
]

FAQ_ITEMS = [
    {
        "icon_class": "fas fa-stethoscope",
        "question": "What services are offered?",
        "answer": "<p>Lab testing, maternal/child health, elder care, and community outreach programs.</p>",
    },
    {
        "icon_class": "fas fa-clock",
        "question": "What are your hours?",
        "answer": "<p>Monday–Friday 7am–6pm, Saturday 8am–4pm. Emergencies are handled 24/7.</p>",
    },
    {
        "icon_class": "fas fa-shield-heart",
        "question": "Do you accept insurance?",
        "answer": "<p>NHIF and major insurers are accepted; affordable plans are available for the uninsured.</p>",
    },
    {
        "icon_class": "fas fa-calendar-check",
        "question": "How do I book an appointment?",
        "answer": "<p>Call, visit, or use the online form. Walk‑ins are welcome.</p>",
    },
]

# NOTE — a real inconsistency in the live template, imported as-is from the
# *visible* set: the team carousel duplicates all nine cards to fake a seamless
# loop, and the two copies disagree about Kulwa Cherehani. The first (labelled)
# card says "Lab Manager"; the aria-hidden duplicate says "Nurse Midwife". Both
# are on screen as the carousel scrolls, so today the site shows her with two
# different roles. Seeded with "Lab Manager" (the primary, non-duplicate set) —
# flagged for AGKI to correct in the admin if that is the wrong one. After
# Phase 6 the duplicate set is generated from this single row, so the
# contradiction disappears either way.
TEAM_MEMBERS = [
    {"asset": ("static:app/TAg.jpg", "cms/team/TAg.jpg"), "name": "Agnes Kimweri", "role": "CEO & Founder"},
    {"asset": ("static:app/Tgloria.jpeg", "cms/team/Tgloria.jpeg"), "name": "Eng. Gloria Jombo", "role": "CTO"},
    {"asset": ("static:app/Tmanager.jpg", "cms/team/Tmanager.jpg"), "name": "Dr Atieno Uyugi", "role": "Co‑founder, Ops"},
    {"asset": ("static:app/Twilson.jpg", "cms/team/Twilson.jpg"), "name": "Hape Wilson", "role": "Lab scientist"},
    {"asset": ("static:app/Tkulwa.png", "cms/team/Tkulwa.png"), "name": "Kulwa Cherehani", "role": "Lab Manager"},
    {"asset": ("static:app/TPau.jpeg", "cms/team/TPau.jpeg"), "name": "Pau Gimeno", "role": "Advisor, fundraising"},
    {
        "asset": ("static:app/TElisabet.jpg", "cms/team/TElisabet.jpg"),
        "name": "Elisabet Nyquist",
        "role": "Business advisor",
    },
    {
        "asset": ("static:app/TDeogratias.jpg", "cms/team/TDeogratias.jpg"),
        "name": "Dr Deogratias Mbona",
        "role": "Advisor",
    },
    {"asset": ("static:app/Tcoln.jpg", "cms/team/Tcoln.jpg"), "name": "Cornel Mtavangu", "role": "ICT Officer"},
]
