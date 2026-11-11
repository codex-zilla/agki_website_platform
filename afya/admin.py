"""Staff screens: verify providers, handle bookings, issue vouchers, see gaps."""

from django import forms
from django.contrib import admin, messages
from django.shortcuts import render
from django.utils import timezone

from afya import bookings, payments
from afya.ai import safety
from afya.models import (Booking, Client, NeedEvent, Payment, Provider, Service, Voucher,
                         WalletEntry)


class ServiceInline(admin.TabularInline):
    model = Service
    extra = 1


@admin.register(Provider)
class ProviderAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "district", "region", "verification", "council",
                    "licence_number", "is_youth_friendly", "average_rating")
    list_filter = ("verification", "kind", "region", "is_youth_friendly", "handles_emergencies")
    search_fields = ("name", "licence_number", "district")
    readonly_fields = ("verified_at", "verified_by", "rating_total", "rating_count")
    inlines = [ServiceInline]
    actions = ["mark_verified", "mark_suspended"]

    @admin.display(description="Rating")
    def average_rating(self, obj):
        return f"{obj.rating_total / obj.rating_count:.1f}" if obj.rating_count else "-"

    @admin.action(description="Mark verified (licence checked against council register)")
    def mark_verified(self, request, queryset):
        for provider in queryset:
            provider.verify(request.user)
        self.message_user(request, f"{queryset.count()} provider(s) verified.")

    @admin.action(description="Suspend (stop matching women to them)")
    def mark_suspended(self, request, queryset):
        count = queryset.update(verification=Provider.Verification.SUSPENDED)
        self.message_user(request, f"{count} provider(s) suspended.")

    def get_readonly_fields(self, request, obj=None):
        # Verification goes through the actions above, so who verified and
        # when is always recorded.
        fields = list(super().get_readonly_fields(request, obj))
        return fields + ["verification"] if obj else fields

    def changelist_view(self, request, extra_context=None):
        if not safety.is_reviewed():
            messages.warning(
                request,
                f"Danger-sign rules version {safety.RULES_VERSION} have not been signed off "
                "by a clinician yet. See afya/ai/safety.py, CLINICAL_REVIEW.",
            )
        return super().changelist_view(request, extra_context)


class VisitTimeForm(forms.Form):
    _selected_action = forms.CharField(widget=forms.MultipleHiddenInput)
    visit_at = forms.DateTimeField(
        label="Visit date and time",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        input_formats=["%Y-%m-%dT%H:%M"],
    )


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ("reference", "provider", "service", "status", "pay_with", "visit_at",
                    "channel", "created_at")
    list_filter = ("status", "pay_with", "channel", "provider__region")
    search_fields = ("reference", "provider__name")
    # Status and payment move only through the actions below, which text her
    # and settle the payment; editing them by hand would skip both.
    readonly_fields = ("reference", "client", "provider", "service", "channel", "status",
                       "pay_with", "voucher", "created_at", "updated_at", "rating")
    actions = ["confirm", "mark_attended", "mark_no_show", "cancel"]

    def has_add_permission(self, request):
        return False  # bookings are made by her, on her phone

    def _each(self, request, queryset, operation):
        done = 0
        for booking in queryset:
            try:
                operation(booking)
                done += 1
            except bookings.BookingError as problem:
                self.message_user(request, str(problem), messages.ERROR)
        if done:
            self.message_user(request, f"{done} booking(s) updated.")

    @admin.action(description="Confirm with a visit time (texts her)")
    def confirm(self, request, queryset):
        if "apply" in request.POST:
            form = VisitTimeForm(request.POST)
            if form.is_valid():
                when = timezone.make_aware(form.cleaned_data["visit_at"]) \
                    if timezone.is_naive(form.cleaned_data["visit_at"]) \
                    else form.cleaned_data["visit_at"]
                self._each(request, queryset, lambda b: bookings.confirm(b, when))
                return None
        else:
            form = VisitTimeForm(initial={"_selected_action": request.POST.getlist(
                admin.helpers.ACTION_CHECKBOX_NAME)})
        return render(request, "afya/admin_confirm.html",
                      {"form": form, "bookings": queryset, "title": "Confirm bookings"})

    @admin.action(description="Mark attended (collects payment, 5%% capped split)")
    def mark_attended(self, request, queryset):
        self._each(request, queryset, bookings.attended)

    @admin.action(description="Mark did not attend")
    def mark_no_show(self, request, queryset):
        self._each(request, queryset, bookings.no_show)

    @admin.action(description="Cancel (texts her)")
    def cancel(self, request, queryset):
        self._each(request, queryset, bookings.cancel)


class PayoutFilter(admin.SimpleListFilter):
    title = "provider payout"
    parameter_name = "payout"

    def lookups(self, request, model_admin):
        return [("owed", "Still owed to provider"), ("sent", "Paid out")]

    def queryset(self, request, queryset):
        if self.value() == "owed":
            return queryset.filter(status=Payment.Status.PAID, provider_share_tsh__gt=0,
                                   paid_out_at__isnull=True)
        if self.value() == "sent":
            return queryset.filter(paid_out_at__isnull=False)
        return queryset


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("booking", "method", "amount_tsh", "commission_tsh", "provider_share_tsh",
                    "status", "paid_at", "paid_out_at")
    list_filter = ("method", "status", PayoutFilter)
    readonly_fields = [f.name for f in Payment._meta.fields]
    actions = ["retry_payout"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False  # financial records must reconcile

    @admin.action(description="Pay out provider share now (if still owed)")
    def retry_payout(self, request, queryset):
        sent = sum(1 for payment in queryset.select_related("booking__provider")
                   if payments.pay_out(payment))
        owed = queryset.filter(status=Payment.Status.PAID, provider_share_tsh__gt=0,
                               paid_out_at__isnull=True).count()
        self.message_user(request, f"{sent} payout(s) sent. {owed} still owed "
                                   "(check the provider has a payout number).",
                          messages.SUCCESS if not owed else messages.WARNING)


@admin.register(Voucher)
class VoucherAdmin(admin.ModelAdmin):
    list_display = ("code", "sponsor", "value_tsh", "need", "region", "expires_on",
                    "claimed", "redeemed_at")
    list_filter = ("sponsor", "need", "region")
    search_fields = ("code", "sponsor")
    readonly_fields = ("claimed_by", "claimed_phone_hash", "redeemed_at")

    @admin.display(boolean=True)
    def claimed(self, obj):
        return bool(obj.claimed_phone_hash)

    def save_model(self, request, obj, form, change):
        if not obj.code:
            obj.code = Voucher.new_code()
        super().save_model(request, obj, form, change)

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if obj is None and "code" in form.base_fields:
            form.base_fields["code"].required = False
            form.base_fields["code"].help_text = "Leave blank to generate one."
        return form


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    """Deliberately thin: staff see masked numbers and no messages."""

    list_display = ("masked_phone", "language", "region", "district", "has_consented",
                    "reminders_opt_out", "created_at")
    list_filter = ("language", "region", "reminders_opt_out")
    fields = ("language", "region", "district", "age_band", "is_pregnant",
              "reminders_opt_out", "consented_at", "created_at")
    readonly_fields = ("consented_at", "created_at")

    @admin.display(boolean=True)
    def has_consented(self, obj):
        return obj.has_consented


@admin.register(WalletEntry)
class WalletEntryAdmin(admin.ModelAdmin):
    list_display = ("client", "amount_tsh", "kind", "reference", "created_at")
    list_filter = ("kind",)

    def has_change_permission(self, request, obj=None):
        return False  # a ledger is append-only

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(NeedEvent)
class NeedEventAdmin(admin.ModelAdmin):
    list_display = ("created_on", "region", "district", "need", "channel", "matched",
                    "booked", "danger_sign")
    list_filter = ("need", "region", "channel", "matched", "booked", "danger_sign")
    date_hierarchy = "created_on"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
