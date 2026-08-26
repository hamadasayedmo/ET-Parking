from datetime import datetime, time

from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsStaffUser
from accounts.scoping import apply_project_scope

from .kiosk_auth import KioskTokenAuthentication
from .kiosk_utils import resolve_kiosk_device
from .models import KioskCredential, PaymentIntent
from .nearpay import NearPayConfigError, mint_nearpay_jwt
from .pairing import mint_pairing_code, redeem_pairing_code
from .permissions import IsKioskCredential, IsKioskOrStaff
from .serializers import (
    CompletePaymentSerializer,
    KioskCredentialSerializer,
    MintPairingCodeSerializer,
    PaymentIntentSerializer,
    RedeemPairingCodeSerializer,
    StaffPaymentIntentSerializer,
)
from .services import (
    apply_payment_result,
    create_payment_intent_for_access_request,
)


def parse_query_datetime(value: str | None, *, end_of_day: bool = False):
    if not value:
        return None
    raw = str(value).strip()
    day = parse_date(raw)
    if day is not None and "T" not in raw and " " not in raw:
        clock = time.max.replace(microsecond=0) if end_of_day else time.min
        return timezone.make_aware(
            datetime.combine(day, clock),
            timezone.get_current_timezone(),
        )
    dt = parse_datetime(raw)
    if dt is not None:
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, timezone.get_current_timezone())
        return dt
    return None



class StaffCreatePayAtExitIntentView(APIView):
    """Staff can manually create a pay-at-exit intent for a pending exit AR."""

    permission_classes = [IsStaffUser]

    def post(self, request):
        from parking.models import AccessRequest

        ar_id = request.data.get("access_request_id")
        if not ar_id:
            return Response(
                {"detail": "access_request_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            access_request = AccessRequest.objects.select_related(
                "device", "device__site", "device__site__project"
            ).get(pk=ar_id)
        except AccessRequest.DoesNotExist:
            return Response(
                {"detail": "Access request not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        try:
            intent = create_payment_intent_for_access_request(access_request)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            PaymentIntentSerializer(intent).data,
            status=status.HTTP_201_CREATED,
        )


class StaffMintKioskPairingCodeView(APIView):
    """Staff: mint a short one-time code for a kiosk credential / exit device."""

    permission_classes = [IsStaffUser]

    def post(self, request):
        serializer = MintPairingCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        qs = KioskCredential.objects.select_related("device", "device__site")
        try:
            if data.get("credential_id"):
                credential = qs.get(pk=data["credential_id"])
            else:
                credential = qs.get(device_id=data["device_id"])
        except KioskCredential.DoesNotExist:
            return Response(
                {"detail": "Kiosk credential not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if request.user and request.user.is_authenticated:
            from accounts.scoping import user_can_access_project

            project_id = credential.device.site.project_id
            if not user_can_access_project(request.user, project_id):
                return Response(
                    {"detail": "Forbidden project."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        row = mint_pairing_code(
            credential=credential,
            created_by=request.user,
            ttl_seconds=data.get("ttl_seconds", 15 * 60),
        )
        ttl = max(0, int((row.expires_at - row.created_at).total_seconds()))
        return Response(
            {
                "code": row.code,
                "expires_at": row.expires_at,
                "ttl_seconds": ttl,
                "credential_id": credential.pk,
                "label": credential.label,
                "device_id": credential.device_id,
                "device_name": credential.device.name,
                "site_name": credential.device.site.name,
            },
            status=status.HTTP_201_CREATED,
        )


class KioskPairView(APIView):
    """
    Unauthenticated: exchange a pairing code for the long-lived kiosk token.
    Code is single-use and short-lived.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RedeemPairingCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = redeem_pairing_code(serializer.validated_data["code"])
        return Response(payload)


class NearPayJwtView(APIView):
    """Mint a NearPay Terminal SDK JWT. PEM stays on the server."""

    authentication_classes = [KioskTokenAuthentication]
    permission_classes = [IsKioskCredential]

    def _mint(self, request, overrides: dict):
        credential = (
            request.auth if isinstance(request.auth, KioskCredential) else None
        )
        if not credential:
            return Response(
                {
                    "detail": (
                        "NearPay JWT requires a kiosk token; "
                        "Tid comes from the credential's nearpay_terminal_id."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        terminal_id = (credential.nearpay_terminal_id or "").strip()
        if not terminal_id:
            return Response(
                {
                    "detail": (
                        "Kiosk credential has no nearpay_terminal_id "
                        "(set Tid in admin for this exit)."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            token = mint_nearpay_jwt(
                terminal_id=terminal_id,
                client_uuid=overrides.get("client_uuid") or None,
                merchant_uuid=overrides.get("merchant_uuid") or None,
            )
        except NearPayConfigError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"jwt": token, "terminal_id": terminal_id})

    def post(self, request):
        data = request.data if hasattr(request, "data") else {}
        return self._mint(
            request,
            {
                "client_uuid": data.get("client_uuid"),
                "merchant_uuid": data.get("merchant_uuid"),
            },
        )

    def get(self, request):
        return self._mint(
            request,
            {
                "client_uuid": request.query_params.get("client_uuid"),
                "merchant_uuid": request.query_params.get("merchant_uuid"),
            },
        )


class KioskMeView(APIView):
    """Return exit device bound to this kiosk token."""

    authentication_classes = [KioskTokenAuthentication]
    permission_classes = [IsKioskCredential]

    def get(self, request):
        from django.conf import settings

        credential = request.auth
        device = credential.device
        project = device.site.project
        powered = (getattr(settings, "KIOSK_POWERED_BY_NAME", "") or "").strip()
        return Response(
            {
                "device_id": device.pk,
                "site_id": device.site_id,
                "site_name": device.site.name,
                "device_name": device.name,
                "nearpay_terminal_id": credential.nearpay_terminal_id,
                "label": credential.label,
                "project_id": project.pk,
                "project_name": project.name,
                "powered_by_name": powered,
                "receipt_base_url": (
                    getattr(settings, "KIOSK_RECEIPT_BASE_URL", "") or ""
                ).strip().rstrip("/"),
            }
        )


class PublicReceiptView(APIView):
    """
    Public receipt JSON: GET /receipts/?plate=...&pin=...

    Returns only the plate's latest paid receipt when the PIN matches.
    The receipt UI is hosted elsewhere; this endpoint is data-only.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        from payments.receipts import find_latest_receipt_intent, receipt_payload

        plate = request.query_params.get("plate", "")
        pin = request.query_params.get("pin", "")
        intent = find_latest_receipt_intent(plate=plate, pin=pin)
        if intent is None:
            return Response(
                {"detail": "Receipt not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(receipt_payload(intent))


class KioskPendingPaymentsView(APIView):
    """Open pay-at-exit bills for this kiosk's exit device."""

    authentication_classes = [
        KioskTokenAuthentication,
        *APIView.authentication_classes,
    ]
    permission_classes = [IsKioskOrStaff]

    def get(self, request):
        device, _credential, error = resolve_kiosk_device(request)
        if error is not None:
            return error

        qs = (
            PaymentIntent.objects.select_related(
                "session",
                "session__site",
                "project",
                "access_request",
                "access_request__device",
            )
            .filter(
                access_request__device_id=device.pk,
                status__in=(
                    PaymentIntent.Status.CREATED,
                    PaymentIntent.Status.PENDING,
                ),
            )
            .order_by("created_at")
        )
        if request.user and request.user.is_authenticated:
            qs = apply_project_scope(qs, request.user, field="project_id")

        return Response(
            {
                "count": qs.count(),
                "results": PaymentIntentSerializer(qs, many=True).data,
            }
        )


class KioskPaymentCompleteView(APIView):
    """Kiosk reports PSP result for its exit device only."""

    authentication_classes = [
        KioskTokenAuthentication,
        *APIView.authentication_classes,
    ]
    permission_classes = [IsKioskOrStaff]

    def post(self, request, pk):
        device, _credential, error = resolve_kiosk_device(request)
        if error is not None:
            return error

        serializer = CompletePaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        qs = PaymentIntent.objects.select_related(
            "session", "session__site", "project", "access_request"
        ).filter(pk=pk, access_request__device_id=device.pk)
        if request.user and request.user.is_authenticated:
            qs = apply_project_scope(qs, request.user, field="project_id")
        try:
            intent = qs.get()
        except PaymentIntent.DoesNotExist:
            return Response(
                {"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND
            )

        try:
            intent = apply_payment_result(
                intent,
                status=serializer.validated_data["status"],
                provider=serializer.validated_data.get("provider", "") or "",
                provider_transaction_id=serializer.validated_data.get(
                    "provider_transaction_id", ""
                ),
                raw_result=serializer.validated_data.get("raw_result"),
                card_last4=serializer.validated_data.get("card_last4", "") or "",
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(PaymentIntentSerializer(intent).data)


class PaymentIntentViewSet(
    mixins.CreateModelMixin,
    viewsets.ReadOnlyModelViewSet,
):
    """
    Staff list/retrieve/export of pay-at-exit PaymentIntents, plus manual create.
    """

    permission_classes = [IsStaffUser]
    search_fields = (
        "session__plate",
        "merchant_reference_id",
        "provider_transaction_id",
    )
    ordering_fields = ("created_at", "paid_at", "amount")
    ordering = ("-created_at",)

    def get_serializer_class(self):
        if self.action == "create":
            return PaymentIntentSerializer
        return StaffPaymentIntentSerializer

    def get_queryset(self):
        qs = PaymentIntent.objects.select_related(
            "project",
            "session",
            "session__site",
            "access_request",
            "access_request__device",
            "access_request__device__kiosk_credential",
            "access_request__device__site",
        )
        qs = apply_project_scope(qs, self.request.user, field="project_id")

        params = self.request.query_params
        project_id = params.get("project")
        if project_id:
            qs = qs.filter(project_id=project_id)
        status_value = params.get("status")
        if status_value:
            qs = qs.filter(status=status_value)
        device_id = params.get("device")
        if device_id:
            qs = qs.filter(access_request__device_id=device_id)
        credential_id = params.get("credential")
        if credential_id:
            qs = qs.filter(
                access_request__device__kiosk_credential__pk=credential_id
            )
        site_id = params.get("site")
        if site_id:
            qs = qs.filter(session__site_id=site_id)

        paid_after = parse_query_datetime(params.get("paid_at_after"))
        if paid_after:
            qs = qs.filter(paid_at__gte=paid_after)
        paid_before = parse_query_datetime(
            params.get("paid_at_before"), end_of_day=True
        )
        if paid_before:
            qs = qs.filter(paid_at__lte=paid_before)
        created_after = parse_query_datetime(params.get("created_after"))
        if created_after:
            qs = qs.filter(created_at__gte=created_after)
        created_before = parse_query_datetime(
            params.get("created_before"), end_of_day=True
        )
        if created_before:
            qs = qs.filter(created_at__lte=created_before)

        search = (params.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(session__plate__icontains=search)
                | Q(merchant_reference_id__icontains=search)
                | Q(provider_transaction_id__icontains=search)
            )
        return qs

    def create(self, request, *args, **kwargs):
        from parking.models import AccessRequest

        ar_id = request.data.get("access_request_id")
        if not ar_id:
            return Response(
                {"detail": "access_request_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            access_request = AccessRequest.objects.select_related(
                "device", "device__site", "device__site__project"
            ).get(pk=ar_id)
        except AccessRequest.DoesNotExist:
            return Response(
                {"detail": "Access request not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        try:
            intent = create_payment_intent_for_access_request(access_request)
        except ValueError as exc:
            return Response(
                {"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST
            )
        return Response(
            PaymentIntentSerializer(intent).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["get"])
    def export(self, request):
        import csv
        import io

        from django.http import HttpResponse

        qs = self.filter_queryset(self.get_queryset()).order_by("-created_at")
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "id",
                "plate",
                "site",
                "project",
                "device",
                "nearpay_terminal_id",
                "kiosk_label",
                "amount",
                "currency",
                "status",
                "provider",
                "provider_transaction_id",
                "merchant_reference_id",
                "paid_at",
                "created_at",
            ]
        )
        for row in qs.iterator(chunk_size=500):
            device = (
                row.access_request.device if row.access_request_id else None
            )
            cred = None
            if device is not None:
                try:
                    cred = device.kiosk_credential
                except KioskCredential.DoesNotExist:
                    cred = None
            writer.writerow(
                [
                    row.id,
                    row.session.plate if row.session_id else "",
                    row.session.site.name
                    if row.session_id and row.session.site_id
                    else "",
                    row.project.name if row.project_id else "",
                    (device.name or f"Exit #{device.pk}") if device else "",
                    cred.nearpay_terminal_id if cred else "",
                    (cred.label or "") if cred else "",
                    row.amount,
                    row.currency,
                    row.status,
                    row.provider,
                    row.provider_transaction_id,
                    row.merchant_reference_id,
                    row.paid_at.isoformat() if row.paid_at else "",
                    row.created_at.isoformat() if row.created_at else "",
                ]
            )
        response = HttpResponse(buffer.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = (
            'attachment; filename="terminal-payments.csv"'
        )
        return response


class KioskCredentialViewSet(viewsets.ReadOnlyModelViewSet):
    """Staff read-only list of payment terminals (kiosk credentials)."""

    serializer_class = KioskCredentialSerializer
    permission_classes = [IsStaffUser]
    ordering_fields = ("created_at", "last_used_at", "label", "id")
    ordering = ("-created_at",)

    def get_queryset(self):
        qs = KioskCredential.objects.select_related(
            "device",
            "device__site",
            "device__site__project",
        )
        qs = apply_project_scope(
            qs, self.request.user, field="device__site__project_id"
        )
        params = self.request.query_params
        project_id = params.get("project")
        if project_id:
            qs = qs.filter(device__site__project_id=project_id)
        site_id = params.get("site")
        if site_id:
            qs = qs.filter(device__site_id=site_id)
        is_active = params.get("is_active")
        if is_active is not None and is_active != "":
            qs = qs.filter(
                is_active=str(is_active).lower() in ("1", "true", "yes")
            )
        search = (params.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(label__icontains=search)
                | Q(nearpay_terminal_id__icontains=search)
                | Q(device__name__icontains=search)
            )
        return qs

