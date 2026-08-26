from django.contrib import admin, messages
from django.utils import timezone

from .models import KioskCredential, KioskPairingCode, PaymentIntent
from .pairing import mint_pairing_code


class KioskPairingCodeInline(admin.TabularInline):
    model = KioskPairingCode
    extra = 0
    can_delete = False
    readonly_fields = ("code", "expires_at", "used_at", "created_at", "created_by")
    fields = ("code", "expires_at", "used_at", "created_at", "created_by")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(KioskCredential)
class KioskCredentialAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "label",
        "device",
        "nearpay_terminal_id",
        "is_active",
        "last_used_at",
        "created_at",
    )
    list_filter = ("is_active",)
    search_fields = ("label", "nearpay_terminal_id", "token", "device__name")
    autocomplete_fields = ("device",)
    readonly_fields = ("token", "created_at", "last_used_at")
    actions = ("revoke_pairing", "deactivate_credentials", "mint_pairing_codes")
    inlines = (KioskPairingCodeInline,)

    @admin.action(description="Revoke pairing (rotate token — tablet must re-pair)")
    def revoke_pairing(self, request, queryset):
        from .models import generate_kiosk_token

        count = 0
        for cred in queryset:
            cred.token = generate_kiosk_token()
            cred.save(update_fields=["token"])
            # Expire any open pairing codes for this credential.
            KioskPairingCode.objects.filter(
                credential=cred,
                used_at__isnull=True,
            ).update(expires_at=timezone.now())
            count += 1
        self.message_user(
            request,
            (
                f"Revoked pairing for {count} credential(s). "
                "Tablets will return to the pairing screen; mint a new code to re-pair."
            ),
            messages.WARNING,
        )

    @admin.action(description="Deactivate credential (block tablet)")
    def deactivate_credentials(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(
            request,
            f"Deactivated {updated} credential(s). Tablets will lose access until re-activated + re-paired.",
            messages.WARNING,
        )

    @admin.action(description="Mint pairing code (15 min, show once)")
    def mint_pairing_codes(self, request, queryset):
        for cred in queryset:
            row = mint_pairing_code(credential=cred, created_by=request.user)
            self.message_user(
                request,
                (
                    f"{cred}: pairing code {row.code} "
                    f"(expires {timezone.localtime(row.expires_at):%H:%M})"
                ),
                messages.WARNING,
            )

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not change:
            self.message_user(
                request,
                f"Kiosk token created. Prefer pairing codes over copying the token.",
                messages.INFO,
            )


@admin.register(KioskPairingCode)
class KioskPairingCodeAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "credential",
        "expires_at",
        "used_at",
        "created_by",
        "created_at",
    )
    list_filter = ("used_at",)
    search_fields = ("code", "credential__label", "credential__device__name")
    autocomplete_fields = ("credential", "created_by")
    readonly_fields = ("code", "expires_at", "used_at", "created_at")

    def has_add_permission(self, request):
        return False


@admin.register(PaymentIntent)
class PaymentIntentAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "merchant_reference_id",
        "project",
        "session",
        "amount",
        "currency",
        "provider",
        "status",
        "provider_transaction_id",
        "created_at",
        "paid_at",
    )
    list_filter = ("status", "provider", "currency", "project")
    search_fields = (
        "merchant_reference_id",
        "provider_transaction_id",
        "session__plate",
    )
    readonly_fields = (
        "project",
        "session",
        "access_request",
        "amount",
        "currency",
        "merchant_reference_id",
        "pin",
        "provider",
        "provider_transaction_id",
        "status",
        "raw_result",
        "created_at",
        "updated_at",
        "paid_at",
    )
    fields = (
        "project",
        "session",
        "access_request",
        "amount",
        "currency",
        "merchant_reference_id",
        "pin",
        "provider",
        "provider_transaction_id",
        "status",
        "raw_result",
        "created_at",
        "updated_at",
        "paid_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
