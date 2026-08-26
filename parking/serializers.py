from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers

from parking.models import AccessRequest, Event, Session, SessionAuditEntry
from wallets.services import normalize_plate


EXIT_MATCH_AUDIT_FIELD = SessionAuditEntry.EXIT_MATCH_FIELD
EXIT_UNMATCH_AUDIT_FIELD = SessionAuditEntry.EXIT_UNMATCH_FIELD
EXIT_MATCH_STATE_FIELDS = (
    SessionAuditEntry.EXIT_MATCH_FIELD,
    SessionAuditEntry.EXIT_UNMATCH_FIELD,
)


class EventSerializer(serializers.ModelSerializer):
    device_label = serializers.SerializerMethodField()
    site_name = serializers.CharField(source="device.site.name", read_only=True)

    class Meta:
        model = Event
        fields = (
            "id",
            "device",
            "device_label",
            "site_name",
            "plate",
            "vehicle_type",
            "action",
            "decision",
            "created_at",
        )
        read_only_fields = fields

    def get_device_label(self, obj):
        return str(obj.device)


class SessionAuditEntrySerializer(serializers.ModelSerializer):
    actor_username = serializers.CharField(
        source="actor.username",
        read_only=True,
        allow_null=True,
    )

    class Meta:
        model = SessionAuditEntry
        fields = (
            "id",
            "action",
            "field",
            "old_value",
            "new_value",
            "note",
            "actor",
            "actor_username",
            "created_at",
        )
        read_only_fields = fields


class SessionListSerializer(serializers.ModelSerializer):
    site_name = serializers.CharField(source="site.name", read_only=True)
    zone_name = serializers.CharField(source="zone.name", read_only=True, allow_null=True)
    project = serializers.IntegerField(source="site.project_id", read_only=True)
    project_name = serializers.CharField(
        source="site.project.name",
        read_only=True,
    )
    entry_device_label = serializers.SerializerMethodField()
    wallet_id = serializers.SerializerMethodField()
    wallet_name = serializers.SerializerMethodField()
    wallet_exempted = serializers.SerializerMethodField()
    exit_matched = serializers.SerializerMethodField()
    exit_match_ocr = serializers.SerializerMethodField()
    exit_match_access_request_id = serializers.SerializerMethodField()
    can_unmatch_exit = serializers.SerializerMethodField()
    can_download_bill = serializers.SerializerMethodField()

    class Meta:
        model = Session
        fields = (
            "id",
            "plate",
            "site",
            "site_name",
            "zone",
            "zone_name",
            "parent_session",
            "grace_exit_until",
            "paid_exit_until",
            "nested_settled_at",
            "project",
            "project_name",
            "entry_device",
            "entry_device_label",
            "start_event",
            "start_time",
            "end_event",
            "end_time",
            "fee",
            "payment_status",
            "billing_method",
            "billing_exempt",
            "waiver_kind",
            "allow_negative_balance",
            "wallet_id",
            "wallet_name",
            "wallet_exempted",
            "exit_matched",
            "exit_match_ocr",
            "exit_match_access_request_id",
            "can_unmatch_exit",
            "can_download_bill",
        )
        read_only_fields = fields

    def get_entry_device_label(self, obj):
        return str(obj.entry_device) if obj.entry_device_id else None

    def _vehicle_for_plate(self, plate: str):
        cache = getattr(self, "_vehicle_cache", None)
        if cache is None:
            cache = {}
            self._vehicle_cache = cache
        if plate not in cache:
            from wallets.models import Vehicle

            cache[plate] = (
                Vehicle.objects.select_related("wallet")
                .filter(plate=plate, is_active=True)
                .first()
            )
        return cache[plate]

    def get_wallet_id(self, obj):
        if obj.wallet_id:
            return obj.wallet_id
        vehicle = self._vehicle_for_plate(obj.plate)
        return vehicle.wallet_id if vehicle else None

    def get_wallet_name(self, obj):
        if obj.wallet_id:
            return obj.wallet.name
        vehicle = self._vehicle_for_plate(obj.plate)
        return vehicle.wallet.name if vehicle else None

    def get_wallet_exempted(self, obj):
        if obj.wallet_id:
            return bool(obj.wallet.is_active and obj.wallet.is_exempted)
        vehicle = self._vehicle_for_plate(obj.plate)
        return bool(
            vehicle and vehicle.wallet.is_active and vehicle.wallet.is_exempted
        )

    def get_exit_matched(self, obj):
        annotated = getattr(obj, "_exit_match_state", None)
        if annotated is not None:
            return annotated == EXIT_MATCH_AUDIT_FIELD
        from parking.services import session_has_active_exit_match

        return session_has_active_exit_match(obj.pk)

    def get_exit_match_ocr(self, obj):
        if not self.get_exit_matched(obj):
            return None
        annotated = getattr(obj, "_exit_match_ocr", None)
        if annotated is not None:
            return annotated or None
        from parking.services import find_exit_match_audit

        entry = find_exit_match_audit(session_id=obj.pk)
        return (entry.old_value if entry else None) or None

    def get_exit_match_access_request_id(self, obj):
        if not self.get_exit_matched(obj):
            return None
        from parking.services import find_exit_match_audit
        import re

        entry = find_exit_match_audit(session_id=obj.pk)
        if entry is None:
            return None
        match = re.search(r"access request #(\d+)", entry.note or "")
        return int(match.group(1)) if match else None

    def get_can_unmatch_exit(self, obj):
        """Open stay with an active operator exit match can always be unmatched."""
        if obj.end_time is not None:
            return False
        return self.get_exit_matched(obj)

    def get_can_download_bill(self, obj):
        from parking.session_bill import session_can_download_bill

        return session_can_download_bill(obj)


class SessionLinkedAccessRequestSerializer(serializers.ModelSerializer):
    """Compact AR rows nested on session detail (avoid heavy gate-control fields)."""

    device_label = serializers.SerializerMethodField()
    decided_by_username = serializers.CharField(
        source="decided_by.username",
        read_only=True,
        allow_null=True,
    )

    class Meta:
        model = AccessRequest
        fields = (
            "id",
            "device",
            "device_label",
            "action",
            "status",
            "reason",
            "decision_note",
            "exempted",
            "decided_by_username",
            "decided_at",
            "resolution_event",
            "created_at",
        )
        read_only_fields = fields

    def get_device_label(self, obj):
        return str(obj.device)


class SessionSerializer(SessionListSerializer):
    start_event_detail = EventSerializer(source="start_event", read_only=True)
    end_event_detail = EventSerializer(source="end_event", read_only=True)
    audit_entries = SessionAuditEntrySerializer(many=True, read_only=True)
    transactions = serializers.SerializerMethodField()
    access_requests = serializers.SerializerMethodField()

    class Meta(SessionListSerializer.Meta):
        fields = SessionListSerializer.Meta.fields + (
            "start_event_detail",
            "end_event_detail",
            "audit_entries",
            "transactions",
            "access_requests",
        )

    def get_transactions(self, obj):
        from wallets.serializers import TransactionSerializer

        qs = obj.transactions.select_related(
            "wallet",
            "wallet__project",
            "created_by",
        ).order_by("-created_at")
        return TransactionSerializer(qs, many=True).data

    def get_access_requests(self, obj):
        from datetime import timedelta

        from django.db.models import Q
        from django.utils import timezone

        event_ids = [
            eid for eid in (obj.start_event_id, obj.end_event_id) if eid
        ]
        window_end = obj.end_time or timezone.now()
        qs = (
            AccessRequest.objects.filter(
                Q(resolution_event_id__in=event_ids)
                | Q(
                    plate=obj.plate,
                    device__site_id=obj.site_id,
                    created_at__gte=obj.start_time - timedelta(minutes=15),
                    created_at__lte=window_end + timedelta(hours=2),
                )
            )
            .select_related("device", "decided_by")
            .distinct()
            .order_by("created_at")
        )
        return SessionLinkedAccessRequestSerializer(qs, many=True).data


class SessionUpdateSerializer(serializers.Serializer):
    plate = serializers.CharField(max_length=32, required=False)
    billing_exempt = serializers.BooleanField(required=False)
    note = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_plate(self, value):
        cleaned = normalize_plate(value)
        if not cleaned:
            raise serializers.ValidationError("Plate cannot be empty.")
        return cleaned


class SessionEndSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, default="")
    end_time = serializers.DateTimeField(required=False, allow_null=True)


class AccessRequestSerializer(serializers.ModelSerializer):
    device_label = serializers.SerializerMethodField()
    site_name = serializers.CharField(source="device.site.name", read_only=True)
    zone_name = serializers.CharField(
        source="device.zone.name", read_only=True, allow_null=True
    )
    project_name = serializers.CharField(
        source="device.site.project.name",
        read_only=True,
    )
    wallet_enabled = serializers.BooleanField(
        source="device.site.project.wallet_enabled",
        read_only=True,
    )
    pay_at_exit_enabled = serializers.BooleanField(
        source="device.site.project.pay_at_exit_enabled",
        read_only=True,
    )
    decided_by_username = serializers.CharField(
        source="decided_by.username",
        read_only=True,
        allow_null=True,
    )
    has_open_session = serializers.SerializerMethodField()
    requires_entry_time = serializers.SerializerMethodField()
    wallet_exempted = serializers.SerializerMethodField()
    wallet_name = serializers.SerializerMethodField()
    can_extend_previous = serializers.SerializerMethodField()
    previous_session = serializers.SerializerMethodField()
    open_payment_intent = serializers.SerializerMethodField()
    billable_open_session = serializers.SerializerMethodField()
    linked_session_id = serializers.SerializerMethodField()
    opened_without_session = serializers.SerializerMethodField()
    exit_matched = serializers.SerializerMethodField()
    exit_match_session_id = serializers.SerializerMethodField()
    exit_match_ocr = serializers.SerializerMethodField()
    is_wallet_fleet_payment = serializers.SerializerMethodField()
    can_validate_payment = serializers.SerializerMethodField()

    class Meta:
        model = AccessRequest
        fields = (
            "id",
            "device",
            "device_label",
            "site_name",
            "zone_name",
            "project_name",
            "wallet_enabled",
            "pay_at_exit_enabled",
            "plate",
            "vehicle_type",
            "action",
            "status",
            "reason",
            "decision_note",
            "exempted",
            "wallet_exempted",
            "wallet_name",
            "decided_by",
            "decided_by_username",
            "decided_at",
            "resolution_event",
            "created_at",
            "has_open_session",
            "requires_entry_time",
            "can_extend_previous",
            "previous_session",
            "open_payment_intent",
            "billable_open_session",
            "linked_session_id",
            "opened_without_session",
            "exit_matched",
            "exit_match_session_id",
            "exit_match_ocr",
            "is_wallet_fleet_payment",
            "can_validate_payment",
        )
        read_only_fields = fields

    def get_device_label(self, obj):
        return str(obj.device)

    def get_linked_session_id(self, obj):
        """Session this AR actually created or closed, when one exists.

        A denied request never touches a stay, and an approved one is only
        linked to a stay that was live at decision time — otherwise an
        unrelated older stay would show up as "the session for this decision".
        """
        cache = getattr(self, "_linked_session_cache", None)
        if cache is None:
            cache = {}
            self._linked_session_cache = cache
        key = (obj.pk, obj.resolution_event_id, obj.plate, obj.device.site_id)
        if key in cache:
            return cache[key]

        session_id = None
        if obj.status == AccessRequest.Status.DENIED:
            cache[key] = None
            return None

        if obj.resolution_event_id:
            session_id = (
                Session.objects.filter(
                    Q(start_event_id=obj.resolution_event_id)
                    | Q(end_event_id=obj.resolution_event_id)
                )
                .values_list("id", flat=True)
                .first()
            )
        if session_id is None:
            open_session = Session.get_open(
                plate=obj.plate, site_id=obj.device.site_id
            )
            if open_session is not None:
                session_id = open_session.pk
        if session_id is None:
            at = obj.decided_at or timezone.now()
            session_id = (
                Session.objects.filter(
                    plate=obj.plate,
                    site_id=obj.device.site_id,
                    start_time__lte=at,
                )
                .filter(Q(end_time__isnull=True) | Q(end_time__gte=at))
                .order_by("-start_time")
                .values_list("id", flat=True)
                .first()
            )
        cache[key] = session_id
        return session_id

    def get_opened_without_session(self, obj):
        """Approved exit that opened the gate without any stay to close."""
        if obj.status != AccessRequest.Status.APPROVED:
            return False
        if obj.action != Event.Action.EXIT:
            return False
        return self.get_linked_session_id(obj) is None

    def _exit_match_info(self, obj):
        cache = getattr(self, "_exit_match_cache", None)
        if cache is None:
            cache = {}
            self._exit_match_cache = cache
        if obj.pk in cache:
            return cache[obj.pk]
        from parking.services import find_exit_match_audit, parse_exit_match_reason

        info = None
        if obj.status == AccessRequest.Status.PENDING and obj.action == Event.Action.EXIT:
            parsed = parse_exit_match_reason(obj.reason)
            audit = find_exit_match_audit(access_request_id=obj.pk)
            if parsed is not None or audit is not None:
                info = {
                    "ocr": parsed[0] if parsed else (audit.old_value if audit else None),
                    "session_id": parsed[1]
                    if parsed
                    else (audit.session_id if audit else None),
                }
        cache[obj.pk] = info
        return info

    def get_exit_matched(self, obj):
        return self._exit_match_info(obj) is not None

    def get_is_wallet_fleet_payment(self, obj):
        if obj.action != Event.Action.EXIT:
            return False
        reason = (obj.reason or "").lower()
        markers = (
            "insufficient wallet",
            "wallet could not cover",
            "wallet / fleet payment",
            "remaining fee due",
        )
        if any(marker in reason for marker in markers):
            return True
        project = obj.device.site.project
        return bool(
            project.wallet_enabled
            and not (project.pay_at_exit_enabled and not project.wallet_enabled)
            and obj.status == AccessRequest.Status.PENDING
            and (
                "awaiting exit payment" not in reason
                and "no kiosk paired" not in reason
            )
            and (
                "insufficient" in reason
                or "wallet" in reason
                or "fleet payment" in reason
            )
        )

    def get_can_validate_payment(self, obj):
        if obj.status != AccessRequest.Status.PENDING:
            return False
        if obj.action != Event.Action.EXIT:
            return False
        project = obj.device.site.project
        if project.pay_at_exit_enabled and not project.wallet_enabled:
            return False
        return self.get_is_wallet_fleet_payment(obj)

    def get_exit_match_session_id(self, obj):
        info = self._exit_match_info(obj)
        return info["session_id"] if info else None

    def get_exit_match_ocr(self, obj):
        info = self._exit_match_info(obj)
        return info["ocr"] if info else None

    def get_has_open_session(self, obj):
        return (
            Session.get_open(plate=obj.plate, site_id=obj.device.site_id) is not None
        )

    def get_requires_entry_time(self, obj):
        from parking.services import access_request_requires_entry_time

        if obj.status != AccessRequest.Status.PENDING:
            return False
        return access_request_requires_entry_time(obj)

    def _vehicle_for_plate(self, obj):
        cache = getattr(self, "_vehicle_cache", None)
        if cache is None:
            cache = {}
            self._vehicle_cache = cache
        plate = obj.plate
        if plate not in cache:
            from wallets.models import Vehicle

            cache[plate] = (
                Vehicle.objects.select_related("wallet")
                .filter(plate=plate, is_active=True)
                .first()
            )
        return cache[plate]

    def get_wallet_exempted(self, obj):
        vehicle = self._vehicle_for_plate(obj)
        return bool(
            vehicle
            and vehicle.wallet.is_active
            and vehicle.wallet.is_exempted
        )

    def get_wallet_name(self, obj):
        vehicle = self._vehicle_for_plate(obj)
        return vehicle.wallet.name if vehicle else None

    def _eligible_previous_session(self, obj):
        cache = getattr(self, "_previous_session_cache", None)
        if cache is None:
            cache = {}
            self._previous_session_cache = cache
        if obj.pk not in cache:
            from payments.services import eligible_previous_paid_session

            cache[obj.pk] = (
                eligible_previous_paid_session(obj)
                if self._is_pending_pay_at_exit(obj)
                else None
            )
        return cache[obj.pk]

    def get_can_extend_previous(self, obj):
        return self._eligible_previous_session(obj) is not None

    def get_previous_session(self, obj):
        session = self._eligible_previous_session(obj)
        if session is None:
            return None
        return {
            "id": session.pk,
            "start_time": session.start_time,
            "paid_exit_until": session.paid_exit_until,
            "fee": str(session.fee) if session.fee is not None else None,
        }

    def _is_pending_pay_at_exit(self, obj) -> bool:
        """Cheap guard so bill lookups never run for entry/decided rows."""
        return (
            obj.status == AccessRequest.Status.PENDING
            and obj.action == Event.Action.EXIT
            and obj.device.site.project.pay_at_exit_enabled
        )

    def get_open_payment_intent(self, obj):
        from decimal import Decimal

        from payments.models import PaymentIntent

        if not self._is_pending_pay_at_exit(obj):
            return None

        intent = (
            obj.payment_intents.filter(
                status__in=(
                    PaymentIntent.Status.CREATED,
                    PaymentIntent.Status.PENDING,
                )
            )
            .select_related("session")
            .order_by("-created_at")
            .first()
        )
        if intent is None:
            return None
        estimated = None
        session = intent.session
        if (
            session is not None
            and session.end_time is None
            and session.payment_status != Session.PaymentStatus.PAID
        ):
            fee = session.estimated_fee()
            if fee > Decimal("0.00"):
                estimated = str(fee)
        return {
            "id": intent.pk,
            "status": intent.status,
            "amount": str(intent.amount),
            "currency": intent.currency,
            "estimated_amount": estimated,
        }

    def get_billable_open_session(self, obj):
        """Open unpaid session that can be re-billed straight to the kiosk."""
        from decimal import Decimal

        from payments.models import PaymentIntent
        from payments.services import project_currency

        if not self._is_pending_pay_at_exit(obj):
            return None
        project = obj.device.site.project
        # Prefer resend over Charge whenever an unpaid open session exists.
        if obj.payment_intents.filter(
            status__in=(
                PaymentIntent.Status.CREATED,
                PaymentIntent.Status.PENDING,
            )
        ).exists():
            return None
        session = Session.get_open(plate=obj.plate, site_id=obj.device.site_id)
        if session is None or session.payment_status == Session.PaymentStatus.PAID:
            return None
        amount = session.estimated_fee()
        if amount <= Decimal("0.00"):
            return None
        last_closed = (
            obj.payment_intents.filter(
                status__in=(
                    PaymentIntent.Status.FAILED,
                    PaymentIntent.Status.CANCELLED,
                )
            )
            .order_by("-updated_at")
            .first()
        )
        previous_amount = (
            str(last_closed.amount) if last_closed is not None else None
        )
        return {
            "id": session.pk,
            "start_time": session.start_time,
            "amount": str(amount),
            "currency": project_currency(project),
            "previous_amount": previous_amount,
        }


class AccessRequestDecisionSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, default="")
    exempted = serializers.BooleanField(required=False, default=False)
    entry_time = serializers.DateTimeField(required=False, allow_null=True)


class AccessRequestSessionMatchSerializer(serializers.Serializer):
    session_id = serializers.IntegerField(min_value=1)


class AccessRequestChargeSerializer(serializers.Serializer):
    mode = serializers.ChoiceField(choices=("extend_previous", "new_session"))
    entry_time = serializers.DateTimeField(required=False, allow_null=True)

    def validate(self, attrs):
        if attrs["mode"] == "new_session" and attrs.get("entry_time") is None:
            raise serializers.ValidationError(
                {"entry_time": "Entry time is required for a new session."}
            )
        return attrs


class ManualPlateSerializer(serializers.Serializer):
    device_id = serializers.IntegerField()
    plate = serializers.CharField(max_length=32)
    note = serializers.CharField(required=False, allow_blank=True, default="")
    exempted = serializers.BooleanField(required=False, default=False)
    entry_time = serializers.DateTimeField(required=False, allow_null=True)
