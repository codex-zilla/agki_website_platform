"""Every word Afya Mfukoni says, in Kiswahili and English.

Kept short: one SMS is 160 characters, and every extra one costs the sender.
"""

from afya.models import Need

TEXT = {
    "consent": {
        "sw": ("Karibu Afya Mfukoni! Tunakusaidia kupata huduma za afya zinazoaminika. "
               "Taarifa zako ni SIRI na hazitauzwa. Jibu NDIYO kukubali, au HAPANA."),
        "en": ("Welcome to Afya Mfukoni! We help you find trusted health care. Your "
               "information is PRIVATE and never sold. Reply YES to agree, or NO."),
    },
    "consent_declined": {
        "sw": "Sawa. Hatutahifadhi taarifa zako. Tuma ujumbe wowote ukiwa tayari.",
        "en": "OK. We will not keep your information. Message us any time you are ready.",
    },
    "ask_need": {
        "sw": ("Eleza unachohitaji kwa maneno yako. Tunasaidia: kliniki ya ujauzito, uzazi wa "
               "mpango, kipimo cha mimba, VVU, magonjwa ya zinaa, hedhi na vipimo."),
        "en": ("Tell us what you need in your own words. We help with: pregnancy care, family "
               "planning, pregnancy tests, HIV, STIs, periods and lab tests."),
    },
    "ask_place": {
        "sw": "Uko wilaya au mkoa gani? Andika jina la wilaya yako.",
        "en": "Which district or region are you in? Type the name of your district.",
    },
    "matches_intro": {
        "sw": "Huduma ya {need} karibu nawe:",
        "en": "{need} near you:",
    },
    "matches_outro": {
        "sw": "Jibu namba ({n}) kuweka miadi kwa siri.",
        "en": "Reply with the number ({n}) to book privately.",
    },
    "no_match": {
        "sw": ("Samahani, bado hatuna mtoa huduma aliyethibitishwa wa {need} karibu nawe. "
               "Tumeweka taarifa hii ili kuongeza huduma eneo lako."),
        "en": ("Sorry, we don't yet have a verified provider for {need} near you. We have "
               "recorded this so we can add services in your area."),
    },
    "ask_phone": {
        "sw": ("Weka namba yako ya simu (tarakimu 10, inaanza na 06 au 07) ili tukutumie "
               "uthibitisho kwa SMS."),
        "en": ("Enter your phone number (10 digits, starting 06 or 07) so we can send your "
               "confirmation by SMS."),
    },
    "otp_sms": {
        "sw": "Afya Mfukoni: namba yako ya uthibitisho ni {code}.",
        "en": "Afya Mfukoni: your confirmation code is {code}.",
    },
    "ask_otp": {
        "sw": "Tumekutumia namba ya tarakimu 4 kwa SMS. Iandike hapa.",
        "en": "We sent a 4-digit code to your phone by SMS. Type it here.",
    },
    "otp_wrong": {
        "sw": "Namba si sahihi. Jaribu tena.",
        "en": "That code is not right. Please try again.",
    },
    "otp_failed": {
        "sw": "Imeshindikana kuthibitisha. Anza tena kwa kueleza unachohitaji.",
        "en": "We couldn't confirm your number. Start again by telling us what you need.",
    },
    "bad_phone": {
        "sw": "Namba hiyo si sahihi. Andika tarakimu 10 zinazoanza na 06 au 07.",
        "en": "That number doesn't look right. Type 10 digits starting 06 or 07.",
    },
    "booked": {
        "sw": ("Miadi imewekwa! Namba: {ref}. {provider}, {district}. {service}: {price}. "
               "{hours}Kliniki itakuthibitishia muda kwa SMS."),
        "en": ("Booked! Ref: {ref}. {provider}, {district}. {service}: {price}. "
               "{hours}The clinic will confirm your time by SMS."),
    },
    # Only added when the provider has listed opening hours.
    "hours": {"sw": "Saa: {hours}. ", "en": "Hours: {hours}. "},
    "booked_voucher": {
        "sw": " Vocha yako imetumika: huduma hii ni BURE kwako.",
        "en": " Your voucher has been applied: this service is FREE for you.",
    },
    "booked_wallet": {
        "sw": " Italipwa kutoka Afya Wallet yako (salio TSh {balance}).",
        "en": " It will be paid from your Afya Wallet (balance TSh {balance}).",
    },
    "confirmed": {
        "sw": "Miadi {ref} imethibitishwa: {when} katika {provider}. Tafadhali fika mapema.",
        "en": "Booking {ref} confirmed: {when} at {provider}. Please arrive a little early.",
    },
    "cancelled": {
        "sw": "Miadi {ref} imeghairiwa. Tuma ujumbe kutafuta huduma nyingine.",
        "en": "Booking {ref} was cancelled. Message us to find another service.",
    },
    "pay_request": {
        "sw": "Lipa TSh {amount} kwa {provider} (miadi {ref}). Thibitisha kwenye simu yako.",
        "en": "Pay TSh {amount} to {provider} (booking {ref}). Approve on your phone.",
    },
    "free": {"sw": "BURE", "en": "FREE"},
    "help": {
        "sw": ("Afya Mfukoni: eleza unachohitaji. VOCHA <namba> kutumia vocha. SALIO kuona "
               "Afya Wallet. WEKA <kiasi> kuweka akiba. ACHA kusimamisha jumbe. FUTA "
               "kufuta taarifa zako. ENGLISH kwa Kiingereza."),
        "en": ("Afya Mfukoni: tell us what you need. VOUCHER <code> to use a voucher. "
               "BALANCE for your Afya Wallet. SAVE <amount> to save. STOP to stop "
               "reminders. DELETE to erase your data. SWAHILI for Kiswahili."),
    },
    "stopped": {
        "sw": "Hutapokea tena jumbe za ukumbusho. Tuma ANZA kuziwasha tena.",
        "en": "You will no longer get reminders. Send START to turn them back on.",
    },
    "started": {
        "sw": "Jumbe za ukumbusho zimewashwa.",
        "en": "Reminders are back on.",
    },
    "erased": {
        "sw": "Taarifa zako zimefutwa. Asante kwa kutumia Afya Mfukoni.",
        "en": "Your information has been erased. Thank you for using Afya Mfukoni.",
    },
    "voucher_ok": {
        "sw": "Vocha {code} (TSh {value}) imehifadhiwa. Itatumika kwenye miadi yako ijayo.",
        "en": "Voucher {code} (TSh {value}) saved. It will be used on your next booking.",
    },
    "voucher_bad": {
        "sw": "Samahani, vocha hiyo si sahihi, imeshatumika au imeisha muda.",
        "en": "Sorry, that voucher is not valid, already used or expired.",
    },
    "balance": {
        "sw": "Salio la Afya Wallet: TSh {balance}.",
        "en": "Afya Wallet balance: TSh {balance}.",
    },
    "save_request": {
        "sw": "Tumekutumia ombi la kuweka TSh {amount} kwenye Afya Wallet. Thibitisha kwenye simu.",
        "en": "We sent a request to save TSh {amount} in your Afya Wallet. Approve on your phone.",
    },
    "save_failed": {
        "sw": "Samahani, hatukuweza kutuma ombi la malipo sasa. Jaribu tena baadaye.",
        "en": "Sorry, we couldn't send the payment request right now. Please try later.",
    },
    "otp_wait": {
        "sw": "Tumetuma namba nyingi kwa simu hii. Subiri saa moja kisha ujaribu tena.",
        "en": "We have sent several codes to this number. Please wait an hour and try again.",
    },
    "otp_expired": {
        "sw": "Namba ya uthibitisho imeisha muda. Weka namba yako ya simu tena.",
        "en": "That code has expired. Please enter your phone number again.",
    },
    "service_gone": {
        "sw": "Samahani, huduma hiyo haipatikani tena. Hizi ndizo zilizopo sasa:",
        "en": "Sorry, that service is no longer available. Here is what is available now:",
    },
    "ussd_booked": {
        "sw": "Miadi {ref} imewekwa katika {provider}. Tumekutumia maelezo kwa SMS.",
        "en": "Booking {ref} made at {provider}. We have sent the details by SMS.",
    },
    "slow_down": {
        "sw": "Ujumbe mwingi sana kwa muda mfupi. Subiri kidogo kisha ujaribu tena.",
        "en": "Too many messages in a short time. Please wait a moment and try again.",
    },
    "saved": {
        "sw": "TSh {amount} imewekwa kwenye Afya Wallet. Salio: TSh {balance}.",
        "en": "TSh {amount} saved in your Afya Wallet. Balance: TSh {balance}.",
    },
    "bad_amount": {
        "sw": "Andika WEKA na kiasi cha TSh 500 au zaidi.",
        "en": "Write SAVE and an amount of TSh 500 or more.",
    },
    "remind_visit": {
        "sw": "Ukumbusho: miadi {ref} kesho {when} katika {provider}.",
        "en": "Reminder: booking {ref} tomorrow {when} at {provider}.",
    },
    "remind_antenatal": {
        "sw": ("Mama, kliniki ya ujauzito mapema humlinda wewe na mtoto. Jibu ANC kupata "
               "kliniki karibu nawe."),
        "en": ("Early antenatal visits keep you and your baby safe. Reply ANC to find a "
               "clinic near you."),
    },
    "ask_rating": {
        "sw": "Huduma ya {provider} ilikuwaje? Jibu 1 (mbaya) hadi 5 (nzuri sana).",
        "en": "How was your care at {provider}? Reply 1 (poor) to 5 (excellent).",
    },
    "thanks_rating": {
        "sw": "Asante! Maoni yako yanasaidia wasichana na wanawake wengine.",
        "en": "Thank you! Your feedback helps other girls and women.",
    },
    "nearest_emergency": {
        "sw": "Karibu nawe: {places}.",
        "en": "Near you: {places}.",
    },
    "not_understood_choice": {
        "sw": "Jibu kwa namba 1-{n}, au eleza huduma nyingine unayohitaji.",
        "en": "Reply with a number 1-{n}, or describe another service you need.",
    },
}

NEED_NAMES = {
    "sw": {
        Need.ANTENATAL: "kliniki ya ujauzito",
        Need.PREGNANCY_TEST: "kipimo cha mimba",
        Need.FAMILY_PLANNING: "uzazi wa mpango",
        Need.STI: "magonjwa ya zinaa",
        Need.HIV: "kupima VVU / PrEP / PEP",
        Need.MENSTRUAL: "afya ya hedhi",
        Need.POSTNATAL: "huduma baada ya kujifungua",
        Need.CERVICAL: "uchunguzi wa saratani ya shingo ya kizazi",
        Need.LAB_TEST: "vipimo vya maabara",
        Need.MENTAL_HEALTH: "ushauri wa afya ya akili",
        Need.GENERAL: "kuonana na daktari",
    },
    "en": {need.value: need.label for need in Need},
}

REASON_WORDS = {
    "sw": {"near": "karibu", "free": "bure", "affordable": "bei nafuu",
           "well_rated": "inapendwa", "youth_friendly": "rafiki kwa vijana"},
    "en": {"near": "near", "free": "free", "affordable": "low cost",
           "well_rated": "well rated", "youth_friendly": "youth-friendly"},
}


def say(key, language, **values):
    language = language if language in ("sw", "en") else "sw"
    return TEXT[key][language].format(**values)


def need_name(need, language):
    language = language if language in ("sw", "en") else "sw"
    return NEED_NAMES[language].get(need, need)


def price_text(price, language):
    return say("free", language) if price == 0 else f"TSh {price:,}"
