from django.urls import path

from afya import views

app_name = "afya"

urlpatterns = [
    path("", views.chat_page, name="chat"),
    path("api/chat/", views.chat_api, name="chat_api"),
    path("webhooks/sms/", views.sms_inbound, name="sms_inbound"),
    path("webhooks/ussd/", views.ussd_callback, name="ussd"),
    path("webhooks/whatsapp/", views.whatsapp_webhook, name="whatsapp"),
    path("webhooks/payments/", views.payment_callback, name="payment_callback"),
]
