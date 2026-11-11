"""Beem Africa SMS gateway client.

The notification channel depends on this small provider boundary rather than
on Beem's HTTP details. Replacing Beem later should only require replacing
this module and its settings.

Beem's SMS API uses HTTP Basic authentication with an API key as the username
and a secret key as the password. Its documented send endpoint is
``POST https://apisms.beem.africa/v1/send``.
"""

import base64
import json

import requests
from django.conf import settings

DEFAULT_BASE_URL = "https://apisms.beem.africa"
NATIONAL_LENGTH = 9


class SmsError(RuntimeError):
    """Beem refused the request or could not be reached."""


def normalise(number):
    """Return a Tanzanian mobile number in Beem's ``255...`` format."""
    digits = "".join(character for character in str(number) if character.isdigit())

    if digits.startswith("255"):
        national = digits[3:]
    elif digits.startswith("0"):
        national = digits[1:]
    elif len(digits) == NATIONAL_LENGTH:
        national = digits
    else:
        raise ValueError(f"{number!r} is not a Tanzanian mobile number.")

    if len(national) != NATIONAL_LENGTH or national[0] not in "67":
        raise ValueError(f"{number!r} is not a Tanzanian mobile number.")

    return "255" + national


def _endpoint():
    base = getattr(settings, "BEEM_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    return f"{base}/v1/send"


def _basic_auth(api_key, secret_key):
    credentials = f"{api_key}:{secret_key}".encode("utf-8")
    return "Basic " + base64.b64encode(credentials).decode("ascii")


def send(to, text, reference=""):
    """Submit one SMS to Beem and return Beem's request id."""
    api_key = getattr(settings, "BEEM_API_KEY", "")
    secret_key = getattr(settings, "BEEM_SECRET_KEY", "")
    sender_id = getattr(settings, "BEEM_SENDER_ID", "")
    if not api_key or not secret_key or not sender_id:
        raise SmsError(
            "BEEM_API_KEY, BEEM_SECRET_KEY and BEEM_SENDER_ID are not "
            "configured, so no SMS can be sent. Set them in the environment."
        )

    payload = {
        "source_addr": sender_id,
        "encoding": 0,
        "schedule_time": "",
        "message": text,
        "recipients": [{
            "recipient_id": reference or "agki-sms",
            "dest_addr": normalise(to),
        }],
    }

    try:
        response = requests.post(
            _endpoint(),
            json=payload,
            headers={
                "Authorization": _basic_auth(api_key, secret_key),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=getattr(settings, "BEEM_TIMEOUT", 15),
        )
    except requests.RequestException as unreachable:
        raise SmsError(f"Beem could not be reached: {unreachable}") from unreachable

    if response.status_code >= 400:
        raise SmsError(
            f"Beem refused the message (HTTP {response.status_code}): "
            f"{response.text[:300]}"
        )

    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError) as unreadable:
        raise SmsError(
            f"Beem returned something that is not JSON: {response.text[:300]}"
        ) from unreadable

    if not body.get("successful"):
        reason = body.get("message") or body.get("description") or body
        raise SmsError(f"Beem rejected the message: {reason}")

    request_id = body.get("request_id")
    if request_id in (None, ""):
        raise SmsError(f"Beem accepted nothing: {body}")
    return str(request_id)
