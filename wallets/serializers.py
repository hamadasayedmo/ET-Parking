from decimal import Decimal

from rest_framework import serializers

from accounts.scoping import user_can_access_project
from wallets.models import Transaction, Vehicle, Wallet


class VehicleSerializer(serializers.ModelSerializer):
    wallet_name = serializers.CharField(source="wallet.name", read_only=True)
    project = serializers.IntegerField(source="wallet.project_id", read_only=True)
    project_name = serializers.CharField(
        source="wallet.project.name",
        read_only=True,
    )

    class Meta:
        model = Vehicle
        fields = (
            "id",
            "wallet",
            "wallet_name",
            "project",
            "project_name",
            "plate",
            "note",
            "is_active",
        )

    def validate_wallet(self, wallet):
        if wallet.assigned_user_id:
            raise serializers.ValidationError(
                "Operator wallets cannot own vehicles."
            )
        return wallet


class WalletSerializer(serializers.ModelSerializer):
    vehicles = VehicleSerializer(many=True, read_only=True)
    project_name = serializers.CharField(source="project.name", read_only=True)
    assigned_user_username = serializers.CharField(
        source="assigned_user.username",
        read_only=True,
        allow_null=True,
    )
    is_operator_wallet = serializers.BooleanField(read_only=True)
    last_topup_balance_after = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        read_only=True,
        allow_null=True,
        required=False,
    )
    balance_pct_of_last_topup = serializers.SerializerMethodField()

    class Meta:
        model = Wallet
        fields = (
            "id",
            "project",
            "project_name",
            "name",
            "assigned_user",
            "assigned_user_username",
            "is_operator_wallet",
            "is_active",
            "is_exempted",
            "balance",
            "currency",
            "updated_at",
            "vehicles",
            "last_topup_balance_after",
            "balance_pct_of_last_topup",
        )
        read_only_fields = (
            "balance",
            "updated_at",
            "project_name",
            "vehicles",
            "assigned_user",
            "assigned_user_username",
            "is_operator_wallet",
            "last_topup_balance_after",
            "balance_pct_of_last_topup",
        )

    def validate_project(self, project):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user is not None and not user_can_access_project(user, project.pk):
            raise serializers.ValidationError(
                "You cannot assign wallets to this project."
            )
        return project

    def get_balance_pct_of_last_topup(self, obj):
        ref = getattr(obj, "last_topup_balance_after", None)
        if ref is None:
            return None
        ref_d = Decimal(str(ref))
        if ref_d <= 0:
            return None
        bal = Decimal(str(obj.balance))
        return float((bal * Decimal("100") / ref_d).quantize(Decimal("0.01")))


class WalletAdjustSerializer(serializers.Serializer):
    amount = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal("0.01")
    )
    reference = serializers.CharField(required=False, allow_blank=True, default="")


class TransactionSerializer(serializers.ModelSerializer):
    wallet_name = serializers.CharField(
        source="wallet.name",
        read_only=True,
    )
    project = serializers.IntegerField(
        source="wallet.project_id",
        read_only=True,
    )
    created_by = serializers.IntegerField(
        source="created_by_id",
        read_only=True,
        allow_null=True,
    )
    created_by_username = serializers.SlugRelatedField(
        source="created_by",
        slug_field="username",
        read_only=True,
        allow_null=True,
    )

    class Meta:
        model = Transaction
        fields = (
            "id",
            "wallet",
            "wallet_name",
            "project",
            "amount",
            "balance_after",
            "transaction_type",
            "session",
            "reference",
            "created_by",
            "created_by_username",
            "created_at",
        )
        read_only_fields = fields
