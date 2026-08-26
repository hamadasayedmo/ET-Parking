from django.conf import settings
from django.db import models
import secrets


def generate_terminal_token() -> str:
    """Kept for historical migrations (GeideaTerminal); unused."""
    return secrets.token_urlsafe(32)


def generate_kiosk_token() -> str:
    return secrets.token_urlsafe(32)


class PaymentIntent(models.Model):
    """
    Bill for a pay-at-exit session.
    The platform never charges a card — the exit kiosk uses any PSP and reports the result.
    """

    class Status(models.TextChoices):
        CREATED = "created", "Created"
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.PROTECT,
        related_name="payment_intents",
    )
    session = models.ForeignKey(
        "parking.Session",
        on_delete=models.PROTECT,
        related_name="payment_intents",
    )
    access_request = models.ForeignKey(
        "parking.AccessRequest",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payment_intents",
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="SAR")
    merchant_reference_id = models.CharField(max_length=64, unique=True)
    pin = models.CharField(
        max_length=8,
        blank=True,
        default="",
        help_text="Last 4 digits of the card used to pay (public receipt lookup).",
    )
    # Free-form PSP label from the kiosk (e.g. "nearpay"); blank until complete.
    provider = models.CharField(max_length=32, blank=True, default="")
    provider_transaction_id = models.CharField(
        max_length=128,
        blank=True,
        default="",
        db_index=True,
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.CREATED,
        db_index=True,
    )
    raw_result = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["provider", "provider_transaction_id"]),
        ]

    def __str__(self):
        return f"{self.merchant_reference_id} ({self.status})"


class KioskCredential(models.Model):
    """
    Dedicated API token for one exit kiosk device.
    Store the token on the tablet; set NearPay Tid for this exit.
    """

    device = models.OneToOneField(
        "sites.Device",
        on_delete=models.CASCADE,
        related_name="kiosk_credential",
        help_text="Must be an exit device.",
    )
    token = models.CharField(
        max_length=64,
        unique=True,
        default=generate_kiosk_token,
        editable=False,
        help_text="Secret API token for the kiosk app (prefer pairing codes; do not bake into APK).",
    )
    nearpay_terminal_id = models.CharField(
        max_length=64,
        help_text="NearPay Tid for this exit (used when minting JWT for the kiosk).",
    )
    label = models.CharField(max_length=100, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "kiosk credential"
        verbose_name_plural = "kiosk credentials"

    def __str__(self):
        name = self.label.strip() or f"Kiosk {self.pk}"
        return f"{name} → device {self.device_id}"

    def clean(self):
        from django.core.exceptions import ValidationError
        from sites.models import Device

        super().clean()
        if not (self.nearpay_terminal_id or "").strip():
            raise ValidationError(
                {"nearpay_terminal_id": "NearPay Tid is required for each exit kiosk."}
            )
        if self.device_id:
            if self.device.type != Device.Type.EXIT:
                raise ValidationError(
                    {"device": "Kiosk credentials can only be attached to exit devices."}
                )


def generate_pairing_code() -> str:
    """Short human-typable code (no ambiguous 0/O/1/I)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(6))


class KioskPairingCode(models.Model):
    """
    One-time short code to provision a kiosk tablet with a KioskCredential token.
    Staff mint → tablet POSTs code to /kiosk/pair/ → receives token once.
    """

    credential = models.ForeignKey(
        KioskCredential,
        on_delete=models.CASCADE,
        related_name="pairing_codes",
    )
    code = models.CharField(max_length=8, unique=True, db_index=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="kiosk_pairing_codes",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "kiosk pairing code"
        verbose_name_plural = "kiosk pairing codes"

    def __str__(self):
        state = "used" if self.used_at else "open"
        return f"{self.code} ({state}) → {self.credential_id}"

    @property
    def is_open(self) -> bool:
        from django.utils import timezone

        return self.used_at is None and self.expires_at > timezone.now()
