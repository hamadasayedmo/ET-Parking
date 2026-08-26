from django.contrib import admin

from accounts.admin import scope_admin_queryset
from sites.models import Site

from .models import Project


class SiteInline(admin.TabularInline):
    model = Site
    extra = 0
    fields = ("name", "is_active", "hourly_rate", "additional_fee", "grace_minutes", "iot_thing_name")
    readonly_fields = ("iot_thing_name",)
    show_change_link = True


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "country",
        "is_active",
        "wallet_enabled",
        "pay_at_exit_enabled",
        "grace_minutes",
        "site_count",
        "created_at",
    )
    list_filter = ("is_active", "country", "wallet_enabled", "pay_at_exit_enabled")
    search_fields = ("name", "seller_name", "vat_number")
    inlines = (SiteInline,)
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        (None, {"fields": ("name", "is_active", "country")}),
        (
            "Billing",
            {
                "fields": (
                    "wallet_enabled",
                    "pay_at_exit_enabled",
                    "currency",
                    "grace_minutes",
                ),
                "description": (
                    "Any combination is valid: both on (hybrid), one on, or both off "
                    "(manual-only AccessRequest). Pay-at-exit bills the exit kiosk. "
                    "Grace minutes are the default copied to new sites."
                ),
            },
        ),
        (
            "Tax / receipts",
            {
                "fields": ("seller_name", "vat_number"),
                "description": (
                    "Used for public receipt JSON and ZATCA Phase-1 QR when country is SA."
                ),
            },
        ),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    def get_queryset(self, request):
        return scope_admin_queryset(super().get_queryset(request), request, field="id")

    @admin.display(description="Sites")
    def site_count(self, obj):
        return obj.sites.count()
