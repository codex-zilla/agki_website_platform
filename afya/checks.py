"""Launch checks for Afya Mfukoni: `python manage.py check --deploy`.

Run them on the live server before launch and after every settings change.
Errors (E) mean something is unsafe or broken; warnings (W) must be cleared
before real women use the service. The webhooks also enforce E001 on every
request (see `token_is_safe`), because gunicorn does not run these checks.
"""

from django.conf import settings
from django.core.checks import Error, Tags, Warning, register

#: Values copied from `.env.example` that must never reach production.
PLACEHOLDERS = {"", "replace-with-a-long-random-token", "changeme", "secret", "token"}
MIN_TOKEN_LENGTH = 32


def token_is_safe(token):
    """A real, long random token, not empty or copied from `.env.example`."""
    return token.strip().lower() not in PLACEHOLDERS and len(token) >= MIN_TOKEN_LENGTH


@register(Tags.security, deploy=True)
def webhook_token(app_configs, **kwargs):
    """Every SMS, USSD and payment webhook trusts this token."""
    if settings.DEBUG:
        return []
    if not token_is_safe(getattr(settings, "AFYA_WEBHOOK_TOKEN", "")):
        return [Error(
            "AFYA_WEBHOOK_TOKEN is missing, a placeholder, or shorter than "
            f"{MIN_TOKEN_LENGTH} characters, so anyone could post fake SMS or payments.",
            hint='Generate one: python -c "import secrets; print(secrets.token_urlsafe(32))" '
                 "and set it in .env, then give the same value to your SMS, USSD and "
                 "payment providers.",
            id="afya.E001",
        )]
    return []


@register(Tags.security, deploy=True)
def whatsapp_secret(app_configs, **kwargs):
    """With WhatsApp switched on, Meta's signature must be checkable."""
    if settings.DEBUG or not getattr(settings, "AFYA_WHATSAPP_TOKEN", ""):
        return []
    problems = []
    if not getattr(settings, "AFYA_WHATSAPP_APP_SECRET", ""):
        problems.append(Error(
            "WhatsApp is configured but AFYA_WHATSAPP_APP_SECRET is empty, so every "
            "incoming WhatsApp message will be refused.",
            hint="Copy the App Secret from your Meta app (App settings > Basic).",
            id="afya.E002",
        ))
    if not getattr(settings, "AFYA_WHATSAPP_VERIFY_TOKEN", ""):
        problems.append(Error(
            "WhatsApp is configured but AFYA_WHATSAPP_VERIFY_TOKEN is empty, so Meta "
            "cannot verify the webhook.",
            hint="Choose a random string, set it here and in the Meta webhook settings.",
            id="afya.E003",
        ))
    return problems


@register(Tags.security, deploy=True)
def launch_readiness(app_configs, **kwargs):
    """Things that are fine on a laptop and wrong on the live service."""
    from afya.ai import safety

    problems = []
    if not safety.is_reviewed():
        problems.append(Warning(
            f"Danger-sign rules {safety.RULES_VERSION} have no clinical sign-off.",
            hint="AGKI's medical lead reviews afya/ai/safety.py and fills in CLINICAL_REVIEW.",
            id="afya.W001",
        ))
    gateway = getattr(settings, "AFYA_PAYMENT_GATEWAY", "afya.payments.SandboxGateway")
    if gateway.endswith("SandboxGateway"):
        problems.append(Warning(
            "AFYA_PAYMENT_GATEWAY is the sandbox: payment requests and provider payouts "
            "are recorded but no money moves.",
            hint="Set it to your mobile-money aggregator's MobileMoneyGateway class.",
            id="afya.W002",
        ))
    backends = {c.get("BACKEND", "") for c in getattr(settings, "CACHES", {}).values()}
    if not backends or any(b.endswith("LocMemCache") for b in backends):
        problems.append(Warning(
            "The cache is per-process, so web-chat rate limits, the one-time-code limit "
            "and WhatsApp de-duplication do not hold across several workers.",
            hint="Set CACHES to Redis or django.core.cache.backends.db.DatabaseCache.",
            id="afya.W003",
        ))
    if not all(getattr(settings, n, "") for n in ("BEEM_API_KEY", "BEEM_SECRET_KEY",
                                                   "BEEM_SENDER_ID")):
        problems.append(Warning(
            "Beem SMS credentials are not set: replies, booking texts and one-time codes "
            "will not be sent.",
            hint="Set BEEM_API_KEY, BEEM_SECRET_KEY and BEEM_SENDER_ID in .env.",
            id="afya.W004",
        ))
    return problems
