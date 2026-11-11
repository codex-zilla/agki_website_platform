"""HTTP endpoints: SMS, USSD and WhatsApp webhooks, web chat, payment callbacks.

Webhooks are called by gateways, not browsers, so they are CSRF-exempt and
instead must present `AFYA_WEBHOOK_TOKEN` (as `?token=` or an `X-Afya-Token`
header). WhatsApp additionally checks Meta's request signature when
`AFYA_WHATSAPP_APP_SECRET` is set.
"""

import hashlib
import hmac
import json
import logging
import secrets

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from afya import checks, guide, outbound, payments
from afya.channels import ussd
from afya.messages import say
from afya.models import Channel, Client

log = logging.getLogger("afya")

MAX_MESSAGE_LENGTH = 500
#: Web chat: at most this many messages per browser session per minute.
WEB_MESSAGES_PER_MINUTE = 20


def _answer(phone, text, channel):
    """Run the guide, and never let one bad message break a webhook.

    A gateway that gets a 500 sends the same message again, which would
    repeat whatever half-happened. If something goes wrong she still gets a
    reply that points her to emergency care.
    """
    try:
        return guide.converse(phone, text, channel)
    except Exception:  # noqa: BLE001
        log.exception("Guide failed on a %s message", channel)
        emergency = getattr(settings, "AFYA_EMERGENCY_NUMBER", "112")
        return (f"Samahani, kuna tatizo. Jaribu tena baadaye. Dharura: piga {emergency}. / "
                f"Sorry, something went wrong. Emergency: call {emergency}."), phone


def _allow(key, limit, seconds):
    """A small fixed-window rate limit on the configured cache."""
    cache.add(key, 0, seconds)
    try:
        return cache.incr(key) <= limit
    except ValueError:
        cache.set(key, 1, seconds)
        return True


def _token_ok(request):
    expected = getattr(settings, "AFYA_WEBHOOK_TOKEN", "")
    if not expected:
        # No token configured: only allowed while developing.
        return settings.DEBUG
    if not settings.DEBUG and not checks.token_is_safe(expected):
        # A placeholder or short token protects nothing: refuse until fixed.
        log.error("AFYA_WEBHOOK_TOKEN is unsafe; webhooks refused (see check --deploy)")
        return False
    given = request.GET.get("token") or request.headers.get("X-Afya-Token", "")
    return hmac.compare_digest(given, expected)


def _field(data, *names):
    for name in names:
        value = data.get(name)
        if value:
            return str(value)
    return ""


def _phone(raw):
    return guide.parse_phone(raw)


@csrf_exempt
@require_POST
def sms_inbound(request):
    """Two-way SMS (Beem, Africa's Talking or any gateway that posts from/text)."""
    if not _token_ok(request):
        return HttpResponseForbidden("bad token")
    data = request.POST
    if not data and request.body:
        try:
            data = json.loads(request.body)
        except ValueError:
            data = {}
    phone = _phone(_field(data, "from", "msisdn", "source_addr", "phone", "phoneNumber"))
    text = _field(data, "text", "message", "body", "content")[:MAX_MESSAGE_LENGTH]
    if not phone:
        return JsonResponse({"error": "no valid sender"}, status=400)
    reply, _ = _answer(phone, text, Channel.SMS)
    outbound.send_sms(phone, reply)
    return JsonResponse({"reply": reply})


@csrf_exempt
@require_POST
def ussd_callback(request):
    if not _token_ok(request):
        return HttpResponseForbidden("bad token")
    phone = _phone(request.POST.get("phoneNumber", ""))
    if not phone:
        return HttpResponse("END Namba si sahihi / Invalid number", content_type="text/plain")
    reply = ussd.respond(request.POST.get("sessionId", ""), phone,
                         request.POST.get("text", "")[:MAX_MESSAGE_LENGTH])
    return HttpResponse(reply, content_type="text/plain")


def _whatsapp_signature_ok(request):
    secret = getattr(settings, "AFYA_WHATSAPP_APP_SECRET", "")
    if not secret:
        # Without the app secret anyone could post fake messages: only allowed
        # while developing.
        return settings.DEBUG
    expected = "sha256=" + hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(request.headers.get("X-Hub-Signature-256", ""), expected)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def whatsapp_webhook(request):
    """Meta WhatsApp Cloud API webhook."""
    if request.method == "GET":
        verify = getattr(settings, "AFYA_WHATSAPP_VERIFY_TOKEN", "")
        if verify and request.GET.get("hub.mode") == "subscribe" and \
                hmac.compare_digest(request.GET.get("hub.verify_token", ""), verify):
            return HttpResponse(request.GET.get("hub.challenge", ""))
        return HttpResponseForbidden("verification failed")

    if not _whatsapp_signature_ok(request):
        return HttpResponseForbidden("bad signature")
    try:
        payload = json.loads(request.body)
        if not isinstance(payload, dict):
            raise ValueError
    except ValueError:
        return JsonResponse({"error": "bad json"}, status=400)

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for message in change.get("value", {}).get("messages", []):
                if message.get("type") != "text":
                    continue
                # Meta delivers at least once: answer each message id only once.
                message_id = str(message.get("id", ""))
                if message_id and not cache.add(f"afya:wa:{message_id}", 1, 24 * 60 * 60):
                    continue
                phone = _phone(message.get("from", ""))
                if not phone:
                    continue
                text = str(message.get("text", {}).get("body", ""))[:MAX_MESSAGE_LENGTH]
                reply, _ = _answer(phone, text, Channel.WHATSAPP)
                outbound.send_whatsapp(phone, reply)
    # Always 200, or Meta retries the same message.
    return JsonResponse({"ok": True})


@require_GET
def chat_page(request):
    return render(request, "afya/chat.html", {
        "emergency_number": getattr(settings, "AFYA_EMERGENCY_NUMBER", "112")})


@require_POST
def chat_api(request):
    """The web chat. A visitor is anonymous until she gives a number to book."""
    try:
        text = str(json.loads(request.body).get("message", ""))[:MAX_MESSAGE_LENGTH]
    except (ValueError, AttributeError):
        return JsonResponse({"error": "bad json"}, status=400)
    if not request.session.session_key:
        request.session.save()
    if not _allow(f"afya:web:{request.session.session_key}", WEB_MESSAGES_PER_MINUTE, 60):
        return JsonResponse({"reply": say("slow_down", "sw") + " / " + say("slow_down", "en")},
                            status=429)
    key = request.session.get("afya_client")
    if not key or not Client.objects.filter(phone=key).exists():
        key = f"web:{secrets.token_urlsafe(12)}"
        request.session["afya_client"] = key
    reply, now_known_as = _answer(key, text, Channel.WEB)
    # Follow her if she gave her number while booking; forget her if she erased.
    if now_known_as:
        request.session["afya_client"] = now_known_as
    else:
        request.session.pop("afya_client", None)
    return JsonResponse({"reply": reply})


@csrf_exempt
@require_POST
def payment_callback(request):
    """Mobile-money result: {"reference": "...", "status": "success" | "failed"}."""
    if not _token_ok(request):
        return HttpResponseForbidden("bad token")
    try:
        data = json.loads(request.body)
        reference = str(data.get("reference", ""))
    except (ValueError, AttributeError):
        return JsonResponse({"error": "bad json"}, status=400)
    succeeded = str(data.get("status", "")).lower() in ("success", "successful", "paid", "completed")

    if reference.startswith("W-"):
        entry = payments.confirm_deposit(reference, succeeded)
        if entry:
            client = entry.client
            outbound.send_sms(client.phone, say(
                "saved", client.language, amount=f"{entry.amount_tsh:,}",
                balance=f"{client.wallet_balance:,}"))
        return JsonResponse({"ok": True, "wallet": bool(entry)})

    payment = payments.confirm_payment(reference, succeeded)
    if payment is None:
        return JsonResponse({"error": "unknown reference"}, status=404)
    return JsonResponse({"ok": True, "status": payment.status})
