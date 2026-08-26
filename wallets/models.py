from decimal import Decimal
from uuid import uuid4

from django.conf import settings
from django.db import models, transaction


class Wallet(models.Model):
    """Named subscriber account: identity + prepaid balance (formerly Client + Wallet).

    When ``assigned_user`` is set this is an *operator wallet* used for cash
    validation overdraft (plates stay on fleet wallets only).
    """

    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.PROTECT,
        related_name="wallets",
    )
    name = models.CharField(max_length=200)
    assigned_user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="operator_wallet",
        help_text=(
            "Staff user who owns this operator cash wallet (one per user). "
            "Create and assign only in Django admin."
        ),
    )
    is_active = models.BooleanField(default=True)
    is_exempted = models.BooleanField(
        default=False,
        help_text="When enabled, all vehicles and sessions for this wallet are billing-exempt.",
    )
    balance = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    currency = models.CharField(max_length=3, default="SAR")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        permissions = (
            ("adjust_wallet", "Can top up or withdraw wallet balances"),
        )

    def __str__(self):
        return f"{self.name} – {self.balance} {self.currency}"

    @property
    def is_operator_wallet(self) -> bool:
        return self.assigned_user_id is not None

    @transaction.atomic
    def deposit(
        self,
        amount: Decimal,
        reference: str | None = None,
        *,
        created_by=None,
    ):
        amount = Decimal(amount)
        if amount <= 0:
            raise ValueError("deposit amount must be positive")

        wallet = Wallet.objects.select_for_update().get(pk=self.pk)
        wallet.balance += amount
        wallet.save(update_fields=["balance", "updated_at"])

        Transaction.objects.create(
            wallet=wallet,
            amount=amount,
            balance_after=wallet.balance,
            transaction_type=Transaction.Type.TOP_UP,
            reference=reference or f"topup-{wallet.pk}-{uuid4().hex[:12]}",
            created_by=created_by,
        )
        self.balance = wallet.balance
        return wallet.balance

    @transaction.atomic
    def withdraw(
        self,
        amount: Decimal,
        *,
        reference: str | None = None,
        session=None,
        transaction_type: str | None = None,
        allow_negative: bool = False,
        created_by=None,
    ) -> bool:
        amount = Decimal(amount)
        if amount <= 0:
            raise ValueError("withdraw amount must be positive")

        wallet = Wallet.objects.select_for_update().get(pk=self.pk)
        if not allow_negative and wallet.balance < amount:
            return False

        wallet.balance -= amount
        wallet.save(update_fields=["balance", "updated_at"])

        Transaction.objects.create(
            wallet=wallet,
            amount=-amount,
            balance_after=wallet.balance,
            transaction_type=transaction_type or Transaction.Type.PARKING_FEE,
            session=session,
            reference=reference or f"withdraw-{wallet.pk}-{uuid4().hex[:12]}",
            created_by=created_by,
        )
        self.balance = wallet.balance
        return True


class Vehicle(models.Model):
    wallet = models.ForeignKey(
        Wallet, on_delete=models.CASCADE, related_name="vehicles"
    )
    plate = models.CharField(max_length=32, unique=True, db_index=True)
    note = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Optional operator note shown on the vehicle card.",
    )
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.plate

    def clean(self):
        from django.core.exceptions import ValidationError

        super().clean()
        if self.wallet_id and self.wallet.assigned_user_id:
            raise ValidationError(
                {"wallet": "Operator wallets cannot own vehicles."}
            )

    def save(self, *args, **kwargs):
        from wallets.services import normalize_plate

        self.plate = normalize_plate(self.plate)
        if self.wallet_id:
            assigned = (
                Wallet.objects.filter(pk=self.wallet_id)
                .values_list("assigned_user_id", flat=True)
                .first()
            )
            if assigned:
                raise ValueError("Operator wallets cannot own vehicles.")
        super().save(*args, **kwargs)


class Transaction(models.Model):
    class Type(models.TextChoices):
        TOP_UP = "top_up", "Top Up"
        PARKING_FEE = "parking_fee", "Parking Fee"
        OPERATOR_VALIDATE = "operator_validate", "Operator validate"
        REFUND = "refund", "Refund"
        WITHDRAW = "withdraw", "Withdraw"

    wallet = models.ForeignKey(
        Wallet,
        on_delete=models.PROTECT,
        related_name="transactions",
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    balance_after = models.DecimalField(max_digits=10, decimal_places=2)
    transaction_type = models.CharField(max_length=20, choices=Type.choices)
    session = models.ForeignKey(
        "parking.Session",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transactions",
    )
    reference = models.CharField(max_length=128, unique=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wallet_transactions",
        help_text="Staff user who topped up or withdrew; blank for system parking fees.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.wallet.name} {self.amount} ({self.transaction_type})"
