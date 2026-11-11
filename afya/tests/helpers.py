from afya import outbound, payments
from afya.models import Client, Need, Provider, Service


def provider(name="Kibaha Clinic", region="Pwani", district="Kibaha", *, kind=Provider.Kind.CLINIC,
             verified=True, services=((Need.FAMILY_PLANNING, 0),), **extra):
    p = Provider.objects.create(
        name=name, kind=kind, region=region, district=district, licence_number="L-1",
        council=Provider.Council.MCT, phone="0712000000",
        verification=Provider.Verification.VERIFIED if verified else Provider.Verification.PENDING,
        opening_hours="Mon-Fri 8-16", **extra,
    )
    for need, price in services:
        Service.objects.create(provider=p, need=need, name_en=need, name_sw=need, price_tsh=price)
    return p


def consented(phone="255712345678", **fields):
    from django.utils import timezone

    return Client.objects.create(phone=phone, consented_at=timezone.now(), **fields)


def reset_outboxes():
    outbound.OUTBOX.clear()
    payments.SandboxGateway.requests.clear()
    payments.SandboxGateway.payouts.clear()
