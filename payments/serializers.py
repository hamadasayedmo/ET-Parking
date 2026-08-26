from rest_framework import serializers

from payments.models import KioskCredential, PaymentIntent


class PaymentIntentSerializer(serializers.ModelSerializer):
    plate = serializers.CharField(source="session.plate", read_only=True)
    site_id = serializers.IntegerField(source="session.site_id", read_only=True)
    site_name = serializers.CharField(source="session.site.name", read_only=True)
    session_start = serializers.DateTimeField(
        source="session.start_time", read_only=True
    )
    session_end = serializers.DateTimeField(
        source="session.end_time", read_only=True, allow_null=True
    )
    paid_exit_until = serializers.DateTimeField(
        source="session.paid_exit_until",
        read_only=True,
        allow_null=True,
    )
    gate_opened = serializers.SerializerMethodField()

    class Meta:
        model = PaymentIntent
        fields = (
            "id",
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
            "plate",
            "site_id",
            "site_name",
            "session_start",
            "session_end",
            "paid_exit_until",
            "gate_opened",
            "created_at",
            "paid_at",
        )
        read_only_fields = fields

    def get_gate_opened(self, obj):
        return bool(getattr(obj, "gate_opened", False))


class StaffPaymentIntentSerializer(PaymentIntentSerializer):
    """List/detail shape for ops Terminals → Payments (includes terminal join)."""

    device_id = serializers.SerializerMethodField()
    device_name = serializers.SerializerMethodField()
    nearpay_terminal_id = serializers.SerializerMethodField()
    kiosk_label = serializers.SerializerMethodField()
    kiosk_credential_id = serializers.SerializerMethodField()
    project_name = serializers.CharField(
        source="project.name", read_only=True, default=""
    )

    class Meta(PaymentIntentSerializer.Meta):
        fields = PaymentIntentSerializer.Meta.fields + (
            "project_name",
            "device_id",
            "device_name",
            "kiosk_credential_id",
            "nearpay_terminal_id",
            "kiosk_label",
        )

    def _device(self, obj):
        ar = obj.access_request
        return ar.device if ar is not None else None

    def _credential(self, obj):
        device = self._device(obj)
        if device is None:
            return None
        try:
            return device.kiosk_credential
        except KioskCredential.DoesNotExist:
            return None

    def get_device_id(self, obj):
        device = self._device(obj)
        return device.pk if device is not None else None

    def get_device_name(self, obj):
        device = self._device(obj)
        if device is None:
            return ""
        return (device.name or "").strip() or f"Exit #{device.pk}"

    def get_kiosk_credential_id(self, obj):
        cred = self._credential(obj)
        return cred.pk if cred is not None else None

    def get_nearpay_terminal_id(self, obj):
        cred = self._credential(obj)
        return cred.nearpay_terminal_id if cred is not None else ""

    def get_kiosk_label(self, obj):
        cred = self._credential(obj)
        return (cred.label or "").strip() if cred is not None else ""


class KioskCredentialSerializer(serializers.ModelSerializer):
    """Read-only staff list — never exposes the kiosk API token."""

    device_name = serializers.SerializerMethodField()
    site_id = serializers.IntegerField(source="device.site_id", read_only=True)
    site_name = serializers.CharField(source="device.site.name", read_only=True)
    project = serializers.IntegerField(
        source="device.site.project_id", read_only=True
    )
    project_name = serializers.CharField(
        source="device.site.project.name", read_only=True
    )

    class Meta:
        model = KioskCredential
        fields = (
            "id",
            "device",
            "device_name",
            "site_id",
            "site_name",
            "project",
            "project_name",
            "nearpay_terminal_id",
            "label",
            "is_active",
            "created_at",
            "last_used_at",
        )
        read_only_fields = fields

    def get_device_name(self, obj):
        device = obj.device
        return (device.name or "").strip() or f"Exit #{device.pk}"


class CompletePaymentSerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        choices=("paid", "failed", "cancelled", "success", "successful", "fail")
    )
    provider = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=32,
        help_text="PSP name used by the kiosk (e.g. nearpay).",
    )
    provider_transaction_id = serializers.CharField(
        required=False, allow_blank=True, default=""
    )
    raw_result = serializers.JSONField(required=False)
    card_last4 = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=19,
        help_text="Last 4 digits of the card used to pay (receipt PIN).",
    )


class MintPairingCodeSerializer(serializers.Serializer):
    credential_id = serializers.IntegerField(required=False)
    device_id = serializers.IntegerField(required=False)
    ttl_seconds = serializers.IntegerField(
        required=False,
        min_value=60,
        max_value=86400,
        default=15 * 60,
    )

    def validate(self, attrs):
        if not attrs.get("credential_id") and not attrs.get("device_id"):
            raise serializers.ValidationError(
                "credential_id or device_id is required."
            )
        return attrs


class RedeemPairingCodeSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=16)
