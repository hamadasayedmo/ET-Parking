from django.db import models


class Project(models.Model):
    """Top-level grouping of parking sites (e.g. a city or operator portfolio)."""

    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)
    wallet_enabled = models.BooleanField(
        default=True,
        help_text="Prepaid wallet / subscription auto-access.",
    )
    pay_at_exit_enabled = models.BooleanField(
        default=False,
        help_text=(
            "Unknown/underfunded plates enter freely; exit pays at the paired "
            "kiosk (NearPay)."
        ),
    )
    currency = models.CharField(
        max_length=3,
        blank=True,
        default="SAR",
        help_text="Currency for pay-at-exit",
    )
    country = models.CharField(
        max_length=2,
        default="SA",
        db_index=True,
        help_text="ISO 3166-1 alpha-2 country code (default SA).",
    )
    seller_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Legal seller name for tax receipts (e.g. ZATCA Phase-1 QR).",
    )
    vat_number = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text="VAT / tax registration number for tax receipts.",
    )
    grace_minutes = models.PositiveIntegerField(
        default=0,
        help_text=(
            "Default free-exit grace (minutes) copied onto new sites. "
            "Site.grace_minutes is what billing uses."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name
