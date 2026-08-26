from decimal import Decimal, InvalidOperation
from uuid import uuid4

from django import forms
from django.contrib import admin, messages
from django.contrib.admin import SimpleListFilter

from accounts.admin import scope_admin_queryset

from .models import Transaction, Vehicle, Wallet
from .services import sync_wallet_session_exemption


class VehicleInline(admin.TabularInline):
    model = Vehicle
    extra = 0
    fields = ("plate", "note", "is_active")


class TransactionInline(admin.TabularInline):
    model = Transaction
    extra = 0
    can_delete = False
    readonly_fields = (
        "created_at",
        "transaction_type",
        "amount",
        "balance_after",
        "reference",
        "session",
        "created_by",
    )

    def has_add_permission(self, request, obj=None):
        return False


class WalletKindFilter(SimpleListFilter):
    title = "wallet kind"
    parameter_name = "kind"

    def lookups(self, request, model_admin):
        return (
            ("client", "Client (fleet)"),
            ("operator", "Operator"),
        )

    def queryset(self, request, queryset):
        if self.value() == "client":
            return queryset.filter(assigned_user__isnull=True)
        if self.value() == "operator":
            return queryset.filter(assigned_user__isnull=False)
        return queryset


class WalletAdminForm(forms.ModelForm):
    top_up_amount = forms.DecimalField(
        required=False,
        min_value=Decimal("0.01"),
        max_digits=10,
        decimal_places=2,
        label="Top up amount",
        help_text="Credit this wallet (leave blank to skip).",
    )
    withdraw_amount = forms.DecimalField(
        required=False,
        min_value=Decimal("0.01"),
        max_digits=10,
        decimal_places=2,
        label="Withdraw amount",
        help_text="Debit this wallet (leave blank to skip).",
    )

    class Meta:
        model = Wallet
        fields = (
            "project",
            "name",
            "assigned_user",
            "is_active",
            "is_exempted",
            "currency",
        )

    def clean(self):
        cleaned = super().clean()
        top_up = cleaned.get("top_up_amount")
        withdraw = cleaned.get("withdraw_amount")
        if top_up and withdraw:
            raise forms.ValidationError(
                "Enter either a top-up or a withdraw amount, not both."
            )
        assigned = cleaned.get("assigned_user")
        if assigned is not None:
            if self.instance.pk and self.instance.vehicles.exists():
                raise forms.ValidationError(
                    {
                        "assigned_user": (
                            "Remove vehicles before converting a client wallet "
                            "into an operator wallet."
                        )
                    }
                )
            if not assigned.is_active:
                raise forms.ValidationError(
                    {"assigned_user": "User is inactive."}
                )
            if not (
                assigned.is_staff
                or assigned.is_superuser
                or assigned.groups.exists()
            ):
                raise forms.ValidationError(
                    {
                        "assigned_user": (
                            "Assigned user must be a staff / ops account."
                        )
                    }
                )
            qs = Wallet.objects.filter(assigned_user=assigned)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise forms.ValidationError(
                    {
                        "assigned_user": (
                            "This user already has an operator wallet."
                        )
                    }
                )
            cleaned["is_exempted"] = False
        return cleaned


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    form = WalletAdminForm
    list_display = (
        "id",
        "name",
        "project",
        "assigned_user",
        "balance",
        "currency",
        "is_active",
        "is_exempted",
        "updated_at",
    )
    list_filter = (WalletKindFilter, "is_active", "is_exempted", "currency", "project")
    search_fields = ("name", "project__name", "assigned_user__username")
    autocomplete_fields = ("project", "assigned_user")
    readonly_fields = ("balance", "updated_at")
    fields = (
        "project",
        "name",
        "assigned_user",
        "is_active",
        "is_exempted",
        "balance",
        "currency",
        "updated_at",
        "top_up_amount",
        "withdraw_amount",
    )

    def get_inlines(self, request, obj=None):
        if obj is not None and obj.assigned_user_id:
            return [TransactionInline]
        return [VehicleInline, TransactionInline]

    def get_queryset(self, request):
        return scope_admin_queryset(super().get_queryset(request), request)

    def save_model(self, request, obj, form, change):
        was_exempted = False
        if change and obj.pk:
            was_exempted = (
                Wallet.objects.filter(pk=obj.pk)
                .values_list("is_exempted", flat=True)
                .first()
                or False
            )
        super().save_model(request, obj, form, change)
        if obj.is_exempted != was_exempted:
            sync_wallet_session_exemption(obj)

        top_up = form.cleaned_data.get("top_up_amount")
        withdraw = form.cleaned_data.get("withdraw_amount")

        if top_up:
            try:
                obj.deposit(
                    Decimal(top_up),
                    reference=f"admin-topup-{obj.pk}-{uuid4().hex[:12]}",
                    created_by=request.user,
                )
            except (ValueError, InvalidOperation) as exc:
                self.message_user(request, str(exc), messages.ERROR)
                return
            self.message_user(
                request,
                f"Topped up {top_up} {obj.currency}. New balance: {obj.balance}.",
                messages.SUCCESS,
            )
            return

        if withdraw:
            ok = obj.withdraw(
                Decimal(withdraw),
                reference=f"admin-withdraw-{obj.pk}-{uuid4().hex[:12]}",
                transaction_type=Transaction.Type.WITHDRAW,
                created_by=request.user,
            )
            if ok:
                self.message_user(
                    request,
                    f"Withdrew {withdraw} {obj.currency}. New balance: {obj.balance}.",
                    messages.SUCCESS,
                )
            else:
                self.message_user(
                    request,
                    f"Insufficient balance to withdraw {withdraw} {obj.currency}.",
                    messages.ERROR,
                )


@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = ("id", "plate", "wallet", "note", "is_active")
    list_filter = ("is_active", "wallet", "wallet__project")
    search_fields = ("plate", "wallet__name", "note")
    autocomplete_fields = ("wallet",)

    def get_queryset(self, request):
        return scope_admin_queryset(
            super().get_queryset(request), request, field="wallet__project_id"
        )


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "wallet",
        "project_name",
        "transaction_type",
        "amount",
        "balance_after",
        "created_by",
        "created_at",
    )
    list_filter = ("transaction_type", "created_at", "wallet__project")
    search_fields = ("wallet__name", "reference", "created_by__username")
    readonly_fields = (
        "wallet",
        "transaction_type",
        "amount",
        "balance_after",
        "reference",
        "session",
        "created_by",
        "created_at",
    )

    def get_queryset(self, request):
        return scope_admin_queryset(
            super().get_queryset(request),
            request,
            field="wallet__project_id",
        ).select_related("created_by", "wallet", "wallet__project")

    @admin.display(ordering="wallet__project__name", description="Project")
    def project_name(self, obj):
        return obj.wallet.project.name

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
