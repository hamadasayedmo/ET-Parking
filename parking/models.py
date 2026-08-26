from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


class Event(models.Model):
    class Action(models.TextChoices):
        ENTRY = "entry", "Entry"
        EXIT = "exit", "Exit"

    class Decision(models.TextChoices):
        GRANTED = "granted", "Granted"
        DENIED = "denied", "Denied"

    device = models.ForeignKey("sites.Device", on_delete=models.PROTECT)
    plate = models.CharField(max_length=32, db_index=True)
    vehicle_type = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text="Optional ANPR vehicle type from the gateway (e.g. car, truck).",
    )
    action = models.CharField(
        max_length=8,
        choices=Action.choices,
        default=Action.ENTRY,
    )
    decision = models.CharField(max_length=32, choices=Decision.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.plate} {self.decision} ({self.action})"


class AccessRequest(models.Model):
    """One pending control-room decision per device (denied auto-access)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        DENIED = "denied", "Denied"
        TIMEOUT = "timeout", "Timeout"

    device = models.ForeignKey(
        "sites.Device",
        on_delete=models.PROTECT,
        related_name="access_requests",
    )
    plate = models.CharField(max_length=32, db_index=True)
    vehicle_type = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text="Optional ANPR vehicle type from the gateway (e.g. car, truck).",
    )
    action = models.CharField(
        max_length=8,
        choices=Event.Action.choices,
        default=Event.Action.ENTRY,
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    reason = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="system reason",
        help_text=(
            "Why the system escalated this request (e.g. no wallet, payment "
            "retry, paid outside grace). Set by the platform — not an operator note."
        ),
    )
    decision_note = models.TextField(
        blank=True,
        default="",
        verbose_name="operator note",
        help_text=(
            "Optional note from the control-room operator when approving or "
            "denying. Separate from the system escalate reason."
        ),
    )
    exempted = models.BooleanField(
        default=False,
        help_text="If approved as exempted, no wallet withdrawal for the session.",
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="access_request_decisions",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    resolution_event = models.OneToOneField(
        Event,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_access_request",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "access request"
        verbose_name_plural = "access requests"
        permissions = [
            ("decide_accessrequest", "Can approve or deny access requests"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["device"],
                condition=models.Q(status="pending"),
                name="uniq_pending_accessrequest_per_device",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["device", "status"]),
        ]

    def __str__(self):
        return f"{self.plate} {self.status} @ device {self.device_id}"


class Session(models.Model):
    class PaymentStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"  # legacy; new settles escalate to AR instead
        EXEMPTED = "exempted", "Exempted"

    class BillingMethod(models.TextChoices):
        WALLET = "wallet", "Wallet"
        CARD = "card", "Card (pay at exit)"

    class WaiverKind(models.TextChoices):
        """Why a stay was not charged (only set when waived / unbillable)."""

        GUEST = "guest", "Guest (no wallet)"
        POLICY = "policy", "Exempt wallet / policy"
        OPERATOR = "operator", "Operator waived"
        GRACE = "grace", "Grace period"

    plate = models.CharField(max_length=32, db_index=True)
    wallet = models.ForeignKey(
        "wallets.Wallet",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sessions",
        help_text="Wallet that owned this plate when the session started (billing owner).",
    )
    site = models.ForeignKey("sites.Site", on_delete=models.PROTECT)
    zone = models.ForeignKey(
        "sites.Zone",
        on_delete=models.PROTECT,
        related_name="sessions",
        null=True,
        blank=True,
        help_text="Billing zone for this visit (from entry device).",
    )
    parent_session = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="child_sessions",
        help_text="Outer session when this visit is nested inside another zone.",
    )
    nested_settled_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When an inner stack exit prepaid this outer session through this time.",
    )
    grace_exit_until = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Outer exit is free until this timestamp after nested settlement.",
    )
    paid_exit_until = models.DateTimeField(
        null=True,
        blank=True,
        help_text="A paid card session may auto-exit through this timestamp.",
    )
    entry_device = models.ForeignKey(
        "sites.Device",
        on_delete=models.PROTECT,
        related_name="entry_sessions",
    )
    start_event = models.OneToOneField(
        Event,
        on_delete=models.PROTECT,
        related_name="started_session",
    )
    start_time = models.DateTimeField()
    end_event = models.OneToOneField(
        Event,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="ended_session",
    )
    end_time = models.DateTimeField(null=True, blank=True)
    fee = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )
    payment_status = models.CharField(
        max_length=16,
        choices=PaymentStatus.choices,
        default=PaymentStatus.PENDING,
        db_index=True,
    )
    billing_method = models.CharField(
        max_length=16,
        choices=BillingMethod.choices,
        default=BillingMethod.WALLET,
        db_index=True,
        help_text="How this session is expected to be settled.",
    )
    billing_exempt = models.BooleanField(
        default=False,
        help_text="Skip wallet withdrawal when the session ends.",
    )
    waiver_kind = models.CharField(
        max_length=16,
        choices=WaiverKind.choices,
        blank=True,
        default="",
        db_index=True,
        help_text=(
            "Why billing was waived: guest (no wallet), policy (exempt wallet), "
            "operator decision, or grace. Empty when not waived."
        ),
    )
    allow_negative_balance = models.BooleanField(
        default=False,
        help_text="Allow parking fee to drive the wallet balance negative.",
    )

    class Meta:
        ordering = ["-start_time"]
        indexes = [
            models.Index(fields=["plate", "end_time"]),
            models.Index(fields=["site", "end_time"]),
            models.Index(fields=["entry_device", "end_time"]),
            models.Index(fields=["wallet", "end_time"]),
        ]

    def __str__(self):
        return f"{self.plate} [{self.start_time} → {self.end_time or 'ongoing'}]"

    @property
    def duration(self):
        if self.end_time:
            return self.end_time - self.start_time
        return None

    @classmethod
    def get_open(cls, *, plate: str, site_id: int):
        """Innermost (latest) open session for plate at site."""
        from wallets.services import normalize_plate

        return (
            cls.objects.select_related("site", "zone", "entry_device")
            .filter(
                plate=normalize_plate(plate),
                site_id=site_id,
                end_time__isnull=True,
            )
            .order_by("-start_time")
            .first()
        )

    @classmethod
    def get_open_stack(cls, *, plate: str, site_id: int) -> list["Session"]:
        """All open sessions for plate at site, innermost first."""
        from wallets.services import normalize_plate

        sessions = list(
            cls.objects.select_related("site", "zone", "entry_device", "parent_session")
            .filter(
                plate=normalize_plate(plate),
                site_id=site_id,
                end_time__isnull=True,
            )
            .order_by("-start_time")
        )
        return sessions

    def within_visit_grace(self, at_time=None) -> bool:
        from datetime import timedelta

        site = self.site
        if site is None:
            return False
        grace = site.effective_grace_minutes()
        if grace <= 0:
            return False
        end = at_time or self.end_time or timezone.now()
        return end <= self.start_time + timedelta(minutes=grace)

    def within_nested_grace(self, at_time=None) -> bool:
        if not self.grace_exit_until:
            return False
        end = at_time or timezone.now()
        return end <= self.grace_exit_until

    def within_paid_exit_grace(self, at_time=None) -> bool:
        if self.payment_status != self.PaymentStatus.PAID or not self.paid_exit_until:
            return False
        return (at_time or timezone.now()) <= self.paid_exit_until

    def estimated_fee(self, at_time=None) -> Decimal:
        from wallets.services import calculate_parking_fee

        end = at_time or self.end_time or timezone.now()
        if self.within_visit_grace(end) or self.within_nested_grace(end):
            return Decimal("0.00")
        if self.nested_settled_at and end > self.nested_settled_at:
            # Residual after nested prepaid through nested_settled_at.
            rate_source = self.zone or self.site
            return calculate_parking_fee(
                self.nested_settled_at, end, rate_source=rate_source
            )
        rate_source = self.zone or self.site
        return calculate_parking_fee(self.start_time, end, rate_source=rate_source)

    def wallet_can_cover(self, wallet, at_time=None) -> bool:
        return wallet.balance >= self.estimated_fee(at_time)


class SessionAuditEntry(models.Model):
    """Operator edits to a session (plate corrections, exemption toggles)."""

    class Action(models.TextChoices):
        PLATE_CHANGED = "plate_changed", "Plate changed"
        EXEMPTED = "exempted", "Marked exempt"
        UNEXEMPTED = "unexempted", "Cleared exemption"
        ENDED = "ended", "Ended manually"
        NOTE = "note", "Note added"
        UPDATED = "updated", "Updated"

    # Audit fields for exit OCR ↔ session matching (never delete; undo appends).
    EXIT_MATCH_FIELD = "exit_access_request"
    EXIT_UNMATCH_FIELD = "exit_access_request_undo"

    session = models.ForeignKey(
        Session,
        on_delete=models.CASCADE,
        related_name="audit_entries",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="session_audit_entries",
    )
    action = models.CharField(max_length=32, choices=Action.choices)
    field = models.CharField(max_length=64, blank=True, default="")
    old_value = models.TextField(blank=True, default="")
    new_value = models.TextField(blank=True, default="")
    note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"session {self.session_id} {self.action} @ {self.created_at}"
