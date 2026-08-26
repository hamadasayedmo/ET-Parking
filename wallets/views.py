from decimal import Decimal
from datetime import datetime, time

from django.db.models import F, OuterRef, Subquery
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from accounts.permissions import CanAdjustWallet, IsStaffUser
from accounts.scoping import apply_project_scope, user_can_access_project

from .models import Transaction, Vehicle, Wallet
from .serializers import (
    TransactionSerializer,
    VehicleSerializer,
    WalletAdjustSerializer,
    WalletSerializer,
)
from .services import normalize_plate, sync_wallet_session_exemption


def parse_query_datetime(value: str | None, *, end_of_day: bool = False):
    if not value:
        return None
    raw = str(value).strip()
    # Prefer date-only handling so end_of_day works (Django's parse_datetime
    # accepts YYYY-MM-DD as midnight).
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


class WalletViewSet(viewsets.ModelViewSet):
    queryset = Wallet.objects.select_related(
        "project", "assigned_user"
    ).prefetch_related("vehicles")
    serializer_class = WalletSerializer
    permission_classes = [IsStaffUser]
    search_fields = ("name", "project__name", "assigned_user__username")
    ordering_fields = ("name", "id")

    def get_queryset(self):
        last_topup = Transaction.objects.filter(
            wallet_id=OuterRef("pk"),
            transaction_type=Transaction.Type.TOP_UP,
        ).order_by("-created_at")
        qs = apply_project_scope(super().get_queryset(), self.request.user).annotate(
            last_topup_balance_after=Subquery(last_topup.values("balance_after")[:1]),
        )
        project_id = self.request.query_params.get("project")
        if project_id:
            qs = qs.filter(project_id=project_id)

        operator = self.request.query_params.get("operator")
        if operator is not None and str(operator).lower() in ("1", "true", "yes"):
            qs = qs.filter(assigned_user__isnull=False)
        elif operator is not None and str(operator).lower() in ("0", "false", "no"):
            qs = qs.filter(assigned_user__isnull=True)

        assigned_user = self.request.query_params.get("assigned_user")
        if assigned_user:
            qs = qs.filter(assigned_user_id=assigned_user)

        # Current balance at/below X% of balance right after the last top-up.
        pct_raw = self.request.query_params.get("max_balance_pct_of_last_topup")
        if pct_raw not in (None, ""):
            try:
                threshold = Decimal(str(pct_raw))
            except Exception:
                threshold = None
            if threshold is not None and threshold >= 0:
                qs = qs.exclude(last_topup_balance_after__isnull=True).filter(
                    last_topup_balance_after__gt=0,
                    balance__lte=F("last_topup_balance_after")
                    * threshold
                    / Decimal("100"),
                )
        return qs

    def perform_create(self, serializer):
        project = serializer.validated_data["project"]
        if not user_can_access_project(self.request.user, project.pk):
            raise PermissionDenied("You cannot create wallets in this project.")
        wallet = serializer.save()
        if wallet.is_exempted:
            sync_wallet_session_exemption(wallet)

    def perform_update(self, serializer):
        project = serializer.validated_data.get("project", self.get_object().project)
        if not user_can_access_project(self.request.user, project.pk):
            raise PermissionDenied("You cannot move wallets into this project.")
        previous = self.get_object()
        was_exempted = previous.is_exempted
        wallet = serializer.save()
        if wallet.is_exempted != was_exempted:
            sync_wallet_session_exemption(wallet)

    @action(detail=True, methods=["post"], permission_classes=[CanAdjustWallet])
    def top_up(self, request, pk=None):
        wallet = self.get_object()
        serializer = WalletAdjustSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        wallet.deposit(
            serializer.validated_data["amount"],
            reference=serializer.validated_data.get("reference") or None,
            created_by=request.user,
        )
        return Response(WalletSerializer(wallet, context={"request": request}).data)

    @action(detail=True, methods=["post"], permission_classes=[CanAdjustWallet])
    def withdraw(self, request, pk=None):
        wallet = self.get_object()
        serializer = WalletAdjustSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ok = wallet.withdraw(
            serializer.validated_data["amount"],
            reference=serializer.validated_data.get("reference") or None,
            transaction_type=Transaction.Type.WITHDRAW,
            created_by=request.user,
        )
        if not ok:
            return Response(
                {"detail": "Insufficient balance."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(WalletSerializer(wallet).data)

    @action(detail=True, methods=["get"], url_path="vehicles/export")
    def vehicles_export(self, request, pk=None):
        import csv
        import io

        from django.http import HttpResponse

        wallet = self.get_object()
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["plate", "is_active"])
        for row in wallet.vehicles.order_by("plate"):
            writer.writerow([row.plate, "1" if row.is_active else "0"])
        response = HttpResponse(buffer.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = (
            f'attachment; filename="wallet-{wallet.pk}-vehicles.csv"'
        )
        return response

    @action(
        detail=True,
        methods=["post"],
        url_path="vehicles/import",
        parser_classes=[JSONParser, FormParser, MultiPartParser],
    )
    def vehicles_import(self, request, pk=None):
        """
        Replace this wallet's vehicles with the CSV plates (all active).

        CSV must include a `plate` column. Existing plates for the wallet that
        are not in the file are removed. Plates listed become active and owned
        by this wallet.
        """
        import csv
        import io

        from django.db import transaction

        wallet = self.get_object()
        if wallet.assigned_user_id:
            return Response(
                {"detail": "Operator wallets cannot own vehicles."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        upload = request.FILES.get("file")
        if upload is None:
            return Response(
                {"detail": "Upload a CSV file as 'file'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            text = upload.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            return Response(
                {"detail": "CSV must be UTF-8 encoded."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            return Response(
                {"detail": "CSV is empty or missing a header row."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        field_map = {name.strip().lower(): name for name in reader.fieldnames}
        if "plate" not in field_map:
            return Response(
                {"detail": "CSV must include a 'plate' column."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        plates: list[str] = []
        seen: set[str] = set()
        errors: list[dict] = []
        for index, raw in enumerate(reader, start=2):
            plate_raw = (raw.get(field_map["plate"]) or "").strip()
            if not plate_raw:
                continue
            plate = normalize_plate(plate_raw)
            if plate in seen:
                continue
            seen.add(plate)
            plates.append(plate)

        if not plates:
            return Response(
                {"detail": "No plates found in CSV."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        for plate in plates:
            vehicle = (
                Vehicle.objects.select_related("wallet").filter(plate=plate).first()
            )
            if (
                vehicle is not None
                and vehicle.wallet_id != wallet.pk
                and not user_can_access_project(
                    request.user, vehicle.wallet.project_id
                )
            ):
                errors.append(
                    {
                        "plate": plate,
                        "detail": "Plate belongs to another project.",
                    }
                )

        if errors:
            return Response(
                {"detail": "Import aborted.", "errors": errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        created = 0
        updated = 0

        with transaction.atomic():
            for plate in plates:
                vehicle = (
                    Vehicle.objects.select_related("wallet")
                    .filter(plate=plate)
                    .first()
                )
                if vehicle is None:
                    Vehicle.objects.create(
                        wallet=wallet, plate=plate, is_active=True
                    )
                    created += 1
                    continue

                changed = False
                if vehicle.wallet_id != wallet.pk:
                    vehicle.wallet = wallet
                    changed = True
                if not vehicle.is_active:
                    vehicle.is_active = True
                    changed = True
                if changed:
                    vehicle.save(update_fields=["wallet", "is_active"])
                    updated += 1
                else:
                    updated += 1

            removed, _ = (
                Vehicle.objects.filter(wallet=wallet)
                .exclude(plate__in=plates)
                .delete()
            )

        return Response(
            {
                "created": created,
                "updated": updated,
                "removed": removed,
                "vehicles": VehicleSerializer(
                    wallet.vehicles.order_by("plate"), many=True
                ).data,
            }
        )


class VehicleViewSet(viewsets.ModelViewSet):
    queryset = Vehicle.objects.select_related("wallet", "wallet__project").all()
    serializer_class = VehicleSerializer
    permission_classes = [IsStaffUser]
    search_fields = ("plate", "wallet__name")
    ordering_fields = ("plate", "id")
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def get_queryset(self):
        qs = apply_project_scope(
            super().get_queryset(), self.request.user, field="wallet__project_id"
        )
        wallet_id = self.request.query_params.get("wallet")
        if wallet_id:
            qs = qs.filter(wallet_id=wallet_id)
        project_id = self.request.query_params.get("project")
        if project_id:
            qs = qs.filter(wallet__project_id=project_id)
        return qs

    def perform_create(self, serializer):
        wallet = serializer.validated_data["wallet"]
        if not user_can_access_project(self.request.user, wallet.project_id):
            raise PermissionDenied("You cannot add vehicles for this wallet.")
        if wallet.assigned_user_id:
            raise PermissionDenied("Operator wallets cannot own vehicles.")
        serializer.save()

    def perform_update(self, serializer):
        wallet = serializer.validated_data.get("wallet", self.get_object().wallet)
        if not user_can_access_project(self.request.user, wallet.project_id):
            raise PermissionDenied("You cannot move vehicles to this wallet.")
        if wallet.assigned_user_id:
            raise PermissionDenied("Operator wallets cannot own vehicles.")
        serializer.save()

    @action(detail=False, methods=["get"])
    def export(self, request):
        import csv
        import io

        from django.http import HttpResponse

        qs = self.filter_queryset(self.get_queryset()).order_by("plate")
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "plate",
                "wallet_id",
                "wallet_name",
                "project_id",
                "project_name",
                "is_active",
            ]
        )
        for row in qs.iterator(chunk_size=500):
            writer.writerow(
                [
                    row.plate,
                    row.wallet_id,
                    row.wallet.name,
                    row.wallet.project_id,
                    row.wallet.project.name,
                    "1" if row.is_active else "0",
                ]
            )
        response = HttpResponse(buffer.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="vehicles.csv"'
        return response

    @action(detail=False, methods=["post"], url_path="import")
    def import_csv(self, request):
        """
        Import plates from CSV.

        Columns (header required): plate, wallet_id | wallet_name, is_active (optional).
        Optional query/body project scopes name lookups when using wallet_name.
        """
        import csv
        import io

        upload = request.FILES.get("file")
        if upload is None:
            return Response(
                {"detail": "Upload a CSV file as 'file'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        project_id = request.query_params.get("project") or request.data.get("project")
        try:
            text = upload.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            return Response(
                {"detail": "CSV must be UTF-8 encoded."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            return Response(
                {"detail": "CSV is empty or missing a header row."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        field_map = {name.strip().lower(): name for name in reader.fieldnames}
        if "plate" not in field_map:
            return Response(
                {"detail": "CSV must include a 'plate' column."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Accept legacy client_* column names during transition.
        has_wallet_id = "wallet_id" in field_map or "client_id" in field_map
        has_wallet_name = "wallet_name" in field_map or "client_name" in field_map
        if not has_wallet_id and not has_wallet_name:
            return Response(
                {"detail": "CSV must include 'wallet_id' or 'wallet_name'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        created = 0
        updated = 0
        skipped = 0
        errors: list[dict] = []

        wallets_qs = apply_project_scope(Wallet.objects.all(), request.user)
        if project_id:
            if not user_can_access_project(request.user, project_id):
                raise PermissionDenied("Forbidden project.")
            wallets_qs = wallets_qs.filter(project_id=project_id)

        for index, raw in enumerate(reader, start=2):
            plate_raw = (raw.get(field_map["plate"]) or "").strip()
            if not plate_raw:
                skipped += 1
                continue
            plate = normalize_plate(plate_raw)

            wallet = None
            id_key = field_map.get("wallet_id") or field_map.get("client_id")
            name_key = field_map.get("wallet_name") or field_map.get("client_name")
            if id_key and (raw.get(id_key) or "").strip():
                try:
                    wid = int(str(raw.get(id_key)).strip())
                except (TypeError, ValueError):
                    errors.append({"row": index, "detail": "Invalid wallet_id."})
                    continue
                wallet = wallets_qs.filter(pk=wid).first()
            elif name_key:
                name = (raw.get(name_key) or "").strip()
                matches = list(wallets_qs.filter(name__iexact=name)[:2])
                if len(matches) == 1:
                    wallet = matches[0]
                elif len(matches) > 1:
                    errors.append(
                        {
                            "row": index,
                            "detail": f"Multiple wallets named '{name}'. Use wallet_id.",
                        }
                    )
                    continue

            if wallet is None:
                errors.append({"row": index, "detail": "Wallet not found."})
                continue

            is_active = True
            if "is_active" in field_map:
                raw_active = (raw.get(field_map["is_active"]) or "1").strip().lower()
                is_active = raw_active in ("1", "true", "yes", "y", "active")

            vehicle = (
                Vehicle.objects.filter(plate=plate).select_related("wallet").first()
            )
            if vehicle is not None:
                if not user_can_access_project(request.user, vehicle.wallet.project_id):
                    errors.append(
                        {
                            "row": index,
                            "detail": f"Plate {plate} exists outside your projects.",
                        }
                    )
                    continue
                vehicle.wallet = wallet
                vehicle.is_active = is_active
                vehicle.save(update_fields=["wallet", "is_active"])
                updated += 1
            else:
                Vehicle.objects.create(wallet=wallet, plate=plate, is_active=is_active)
                created += 1

        return Response(
            {
                "created": created,
                "updated": updated,
                "skipped": skipped,
                "errors": errors[:50],
                "error_count": len(errors),
            }
        )


class TransactionViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Transaction.objects.select_related(
        "wallet",
        "wallet__project",
        "session",
        "created_by",
    ).all()
    serializer_class = TransactionSerializer
    permission_classes = [IsStaffUser]
    search_fields = ("reference", "wallet__name")
    ordering_fields = ("created_at", "amount")
    ordering = ("-created_at",)

    def get_queryset(self):
        qs = apply_project_scope(
            super().get_queryset(),
            self.request.user,
            field="wallet__project_id",
        )
        wallet_id = self.request.query_params.get("wallet")
        if wallet_id:
            qs = qs.filter(wallet_id=wallet_id)
        project_id = self.request.query_params.get("project")
        if project_id:
            qs = qs.filter(wallet__project_id=project_id)
        tx_type = self.request.query_params.get("transaction_type")
        if tx_type:
            qs = qs.filter(transaction_type=tx_type)
        created_after = self.request.query_params.get("created_after")
        if created_after:
            parsed = parse_query_datetime(created_after)
            if parsed:
                qs = qs.filter(created_at__gte=parsed)
        created_before = self.request.query_params.get("created_before")
        if created_before:
            parsed = parse_query_datetime(created_before, end_of_day=True)
            if parsed:
                qs = qs.filter(created_at__lte=parsed)
        return qs

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
                "created_at",
                "wallet_id",
                "wallet_name",
                "project_id",
                "transaction_type",
                "amount",
                "balance_after",
                "plate",
                "session_id",
                "reference",
                "created_by",
            ]
        )
        for row in qs.iterator(chunk_size=500):
            writer.writerow(
                [
                    row.id,
                    row.created_at.isoformat(),
                    row.wallet_id,
                    row.wallet.name,
                    row.wallet.project_id,
                    row.transaction_type,
                    row.amount,
                    row.balance_after,
                    row.session.plate if row.session_id else "",
                    row.session_id or "",
                    row.reference,
                    row.created_by.username if row.created_by_id else "",
                ]
            )
        response = HttpResponse(buffer.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = (
            'attachment; filename="transactions.csv"'
        )
        return response
