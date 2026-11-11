"""Sending messages back to a client on the channel she used.

SMS goes through the platform's existing Beem client. WhatsApp goes through
the Meta WhatsApp Cloud API. When a channel isn't configured (a laptop, the
test suite) the message is kept in `OUTBOX` and never sent.

Logs never contain message text or full numbers: an outgoing message can hold
a one-time code or say what care she asked for, and logs are read by more
people, for longer, than the database.
"""

import logging
from collections import deque

import requests
from django.conf import settings

from afya.models import Channel

log = logging.getLogger("afya")

#: The last messages "sent" while no gateway is configured, newest last. Tests
#: read it. Bounded so a server running without a gateway can't fill memory.
OUTBOX = deque(maxlen=500)


def _tail(phone):
    return f"...{str(phone)[-4:]}"


def sms_configured():
    return all(getattr(settings, name, "") for name in
               ("BEEM_API_KEY", "BEEM_SECRET_KEY", "BEEM_SENDER_ID"))


def whatsapp_configured():
    return bool(getattr(settings, "AFYA_WHATSAPP_TOKEN", "")
                and getattr(settings, "AFYA_WHATSAPP_PHONE_ID", ""))


def send_sms(phone, text, reference="afya"):
    if not phone:
        return None
    if not sms_configured():
        OUTBOX.append((Channel.SMS, phone, text))
        log.info("SMS not sent to %s: gateway not configured (%d chars)", _tail(phone), len(text))
        return None
    from appointments.notifications import beem

    try:
        return beem.send(phone, text, reference=reference)
    except (beem.SmsError, ValueError) as problem:
        log.warning("SMS to %s failed: %s", _tail(phone), problem)
        return None


def send_whatsapp(phone, text):
    if not phone:
        return None
    if not whatsapp_configured():
        OUTBOX.append((Channel.WHATSAPP, phone, text))
        log.info("WhatsApp not sent to %s: not configured (%d chars)", _tail(phone), len(text))
        return None
    version = getattr(settings, "AFYA_WHATSAPP_API_VERSION", "v21.0")
    url = f"https://graph.facebook.com/{version}/{settings.AFYA_WHATSAPP_PHONE_ID}/messages"
    try:
        response = requests.post(
            url,
            json={"messaging_product": "whatsapp", "to": phone, "type": "text",
                  "text": {"body": text}},
            headers={"Authorization": f"Bearer {settings.AFYA_WHATSAPP_TOKEN}"},
            timeout=15,
        )
        response.raise_for_status()
        return response.json().get("messages", [{}])[0].get("id")
    except (requests.RequestException, ValueError) as problem:
        log.warning("WhatsApp to %s failed: %s", _tail(phone), type(problem).__name__)
        return None


def send(client, text, channel=Channel.SMS):
    """Send to a client. Web visitors without a number get nothing pushed."""
    if client is None or client.is_web_visitor:
        return None
    if channel == Channel.WHATSAPP:
        return send_whatsapp(client.phone, text)
    return send_sms(client.phone, text)
