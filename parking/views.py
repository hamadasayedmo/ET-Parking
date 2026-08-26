from datetime import datetime, time, timedelta
import csv
import io

from django.db.models import Count, OuterRef, Q, Subquery, Sum
from django.http import HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response

from accounts.permissions import CanDecideAccessRequest, IsStaffUser
from accounts.scoping import (
    apply_project_scope,
    scoped_project_queryset,
    user_can_access_project,
)
from parking.cashier import (
    cashier_hub_context,
    search_on_site_sessions_for_cashier,
    validate_open_session_for_exit,
)
from parking.services import (
    approve_access_request,
    deny_access_request,
    end_session,
    match_access_request_to_session,
    session_matches_for_access_request,
    settle_session,
    staff_process_plate,
    unmatch_access_request_from_session,
    unmatch_session_exit,
    update_session,
    validate_payment_access_request,
)
from projects.models import Project
from sites.models import Device, Site
from wallets.models import Vehicle, Wallet

from .models import AccessRequest, Event, Session, SessionAuditEntry
from .serializers import (
    AccessRequestChargeSerializer,
    AccessRequestDecisionSerializer,
    AccessRequestSessionMatchSerializer,
    AccessRequestSerializer,
    EventSerializer,
    ManualPlateSerializer,
    SessionListSerializer,
    SessionSerializer,
    SessionUpdateSerializer,
    SessionEndSerializer,
    EXIT_MATCH_AUDIT_FIELD,
    EXIT_MATCH_STATE_FIELDS,
    EXIT_UNMATCH_AUDIT_FIELD,
)


def parse_query_datetime(value: str | None, *, end_of_day: bool = False):
    """Parse ISO datetime or YYYY-MM-DD query params into aware datetimes."""
    if not value:
        return None
    parsed = parse_datetime(value)
    if parsed is not None:
        if timezone.is_naive(parsed):
            return timezone.make_aware(parsed)
        return parsed
    day = parse_date(value)
    if day is None:
        return None
    bound = time.max if end_of_day else time.min
    dt = datetime.combine(day, bound)
    if timezone.is_naive(dt):
        return timezone.make_aware(dt)
    return dt


@api_view(["GET"])
@permission_classes([CanDecideAccessRequest])
def cashier_me(request):
    """Zones assigned to the current user for the Cashier Hub."""
    return Response(cashier_hub_context(request.user))


@api_view(["GET"])
@permission_classes([CanDecideAccessRequest])
def cashier_search(request):
    """On-site plate search in the cashier's zones (exact + nearest matches)."""
    from wallets.services import normalize_plate

    raw = (request.query_params.get("q") or "").strip()
    try:
        limit = int(request.query_params.get("limit") or 8)
    except (TypeError, ValueError):
        limit = 8
    results = search_on_site_sessions_for_cashier(
        request.user, query=raw, limit=limit
    )
    return Response(
        {
            "query": normalize_plate(raw),
            "results": results,
        }
    )


@api_view(["GET"])
@permission_classes([IsStaffUser])
def ops_dashboard(request):
    """Fleet-wide operations snapshot: KPIs, per-project health, control queue."""
    now = timezone.now()
    today_start = timezone.localtime(now).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    projects_qs = scoped_project_queryset(request.user)
    project_id = request.query_params.get("project")
    if project_id:
        if not user_can_access_project(request.user, project_id):
            return Response(
                {"detail": "Forbidden project."},
                status=status.HTTP_403_FORBIDDEN,
            )
        projects_qs = projects_qs.filter(pk=project_id)

    # Live ops view excludes inactive sites (still manageable under Infrastructure).
    active_site = Q(site__is_active=True)
    if project_id:
        active_site &= Q(site__project_id=project_id)

    sessions_qs = apply_project_scope(
        Session.objects.filter(active_site),
        request.user,
        field="site__project_id",
    )
    if project_id:
        sessions_qs = sessions_qs.filter(site__project_id=project_id)
    open_sessions_qs = sessions_qs.filter(end_time__isnull=True)
    sessions_today_qs = sessions_qs.filter(start_time__gte=today_start)
    events_today_qs = apply_project_scope(
        Event.objects.filter(created_at__gte=today_start, device__site__is_active=True),
        request.user,
        field="device__site__project_id",
    )
    if project_id:
        events_today_qs = events_today_qs.filter(device__site__project_id=project_id)
    pending_qs = apply_project_scope(
        AccessRequest.objects.filter(
            status=AccessRequest.Status.PENDING,
            device__site__is_active=True,
        ),
        request.user,
        field="device__site__project_id",
    ).select_related(
        "device",
        "device__site",
        "device__site__project",
        "device__zone",
        "decided_by",
        "resolution_event",
    )
    if project_id:
        pending_qs = pending_qs.filter(device__site__project_id=project_id)
    sites_qs = apply_project_scope(
        Site.objects.filter(is_active=True), request.user
    )
    if project_id:
        sites_qs = sites_qs.filter(project_id=project_id)
    devices_qs = apply_project_scope(
        Device.objects.filter(site__is_active=True),
        request.user,
        field="site__project_id",
    )
    if project_id:
        devices_qs = devices_qs.filter(site__project_id=project_id)

    revenue_today = (
        sessions_today_qs.filter(payment_status=Session.PaymentStatus.PAID).aggregate(
            total=Sum("fee")
        )["total"]
        or 0
    )

    project_rows = []
    for project in projects_qs.annotate(
        site_count_agg=Count(
            "sites",
            filter=Q(sites__is_active=True),
            distinct=True,
        )
    ).order_by("name"):
        p_sessions = Session.objects.filter(
            site__project=project, site__is_active=True
        )
        p_open = p_sessions.filter(end_time__isnull=True).count()
        p_today = p_sessions.filter(start_time__gte=today_start).count()
        p_pending = AccessRequest.objects.filter(
            status=AccessRequest.Status.PENDING,
            device__site__project=project,
            device__site__is_active=True,
        ).count()
        p_devices = Device.objects.filter(
            site__project=project, site__is_active=True
        )
        p_revenue = (
            p_sessions.filter(
                start_time__gte=today_start,
                payment_status=Session.PaymentStatus.PAID,
            ).aggregate(total=Sum("fee"))["total"]
            or 0
        )
        project_rows.append(
            {
                "id": project.id,
                "name": project.name,
                "is_active": project.is_active,
                "sites": project.site_count_agg,
                "devices": p_devices.count(),
                "devices_enabled": p_devices.filter(enabled=True).count(),
                "sessions_open": p_open,
                "sessions_today": p_today,
                "pending_requests": p_pending,
                "revenue_today": f"{p_revenue:.2f}",
            }
        )

    project_rows.sort(
        key=lambda row: (
            row["pending_requests"],
            row["sessions_open"],
            row["sessions_today"],
        ),
        reverse=True,
    )

    open_list = list(
        open_sessions_qs.select_related("site", "site__project", "zone")[:12]
    )
    open_session_payload = SessionListSerializer(open_list, many=True).data
    for item, obj in zip(open_session_payload, open_list):
        item["project_name"] = obj.site.project.name

    recent_events = apply_project_scope(
        Event.objects.filter(device__site__is_active=True).select_related(
            "device", "device__site", "device__site__project"
        ),
        request.user,
        field="device__site__project_id",
    )
    if project_id:
        recent_events = recent_events.filter(device__site__project_id=project_id)
    recent_events = recent_events[:12]

    wallets_qs = apply_project_scope(Wallet.objects.all(), request.user)
    vehicles_qs = apply_project_scope(
        Vehicle.objects.all(), request.user, field="wallet__project_id"
    )
    if project_id:
        wallets_qs = wallets_qs.filter(project_id=project_id)
        vehicles_qs = vehicles_qs.filter(wallet__project_id=project_id)

    trend_7d = []
    for offset in range(6, -1, -1):
        day_start = today_start - timedelta(days=offset)
        day_end = day_start + timedelta(days=1)
        day_sessions = sessions_qs.filter(
            start_time__gte=day_start, start_time__lt=day_end
        )
        day_revenue = (
            day_sessions.filter(payment_status=Session.PaymentStatus.PAID).aggregate(
                total=Sum("fee")
            )["total"]
            or 0
        )
        day_events = apply_project_scope(
            Event.objects.filter(
                created_at__gte=day_start,
                created_at__lt=day_end,
                device__site__is_active=True,
            ),
            request.user,
            field="device__site__project_id",
        )
        if project_id:
            day_events = day_events.filter(device__site__project_id=project_id)
        trend_7d.append(
            {
                "date": day_start.date().isoformat(),
                "label": day_start.strftime("%a"),
                "sessions": day_sessions.count(),
                "revenue": float(day_revenue),
                "events": day_events.count(),
                "granted": day_events.filter(decision=Event.Decision.GRANTED).count(),
                "denied": day_events.filter(decision=Event.Decision.DENIED).count(),
            }
        )

    return Response(
        {
            "generated_at": now.isoformat(),
            "stats": {
                "pending_requests": pending_qs.count(),
                "sessions_open": open_sessions_qs.count(),
                "sessions_today": sessions_today_qs.count(),
                "sessions_total": sessions_qs.count(),
                "events_today": events_today_qs.count(),
                "events_granted_today": events_today_qs.filter(
                    decision=Event.Decision.GRANTED
                ).count(),
                "events_denied_today": events_today_qs.filter(
                    decision=Event.Decision.DENIED
                ).count(),
                "revenue_today": f"{revenue_today:.2f}",
                "revenue_currency": "SAR",
                "projects": projects_qs.count(),
                "projects_active": projects_qs.filter(is_active=True).count(),
                "sites": sites_qs.count(),
                "devices": devices_qs.count(),
                "devices_enabled": devices_qs.filter(enabled=True).count(),
                "wallets": wallets_qs.count(),
                "vehicles": vehicles_qs.count(),
                "payment_paid_today": sessions_today_qs.filter(
                    payment_status=Session.PaymentStatus.PAID
                ).count(),
                "payment_failed_today": sessions_today_qs.filter(
                    payment_status=Session.PaymentStatus.FAILED
                ).count(),
                "payment_exempted_today": sessions_today_qs.filter(
                    payment_status=Session.PaymentStatus.EXEMPTED
                ).count(),
                "payment_guest_today": sessions_today_qs.filter(
                    payment_status=Session.PaymentStatus.EXEMPTED,
                    waiver_kind=Session.WaiverKind.GUEST,
                ).count(),
                "payment_operator_waived_today": sessions_today_qs.filter(
                    payment_status=Session.PaymentStatus.EXEMPTED,
                    waiver_kind=Session.WaiverKind.OPERATOR,
                ).count(),
                "sessions_unpaid_closed": sessions_qs.filter(
                    end_time__isnull=False,
                    payment_status=Session.PaymentStatus.PENDING,
                ).count(),
            },
            "projects": project_rows,
            "trend_7d": trend_7d,
            "pending_requests": AccessRequestSerializer(
                pending_qs[:25], many=True
            ).data,
            "open_sessions": open_session_payload,
            "recent_events": EventSerializer(recent_events, many=True).data,
        }
    )


@api_view(["GET"])
@permission_classes([IsStaffUser])
def ops_vehicles(request):
    """Plates seen across scoped sites (optional ?project=), with latest site context."""
    project_id = request.query_params.get("project")
    if project_id and not user_can_access_project(request.user, project_id):
        return Response(
            {"detail": "Forbidden project."},
            status=status.HTTP_403_FORBIDDEN,
        )

    sessions = apply_project_scope(
        Session.objects.select_related("site", "site__project", "zone").order_by(
            "-start_time"
        ),
        request.user,
        field="site__project_id",
    )
    if project_id:
        sessions = sessions.filter(site__project_id=project_id)

    latest_by_plate = {}
    session_counts: dict[str, int] = {}
    for session in sessions.iterator(chunk_size=500):
        session_counts[session.plate] = session_counts.get(session.plate, 0) + 1
        if session.plate not in latest_by_plate:
            latest_by_plate[session.plate] = session

    plates = list(latest_by_plate.keys())
    vehicle_map = {
        v.plate: v
        for v in Vehicle.objects.filter(plate__in=plates).select_related("wallet")
    }

    search = (request.query_params.get("search") or "").strip().upper()
    site_id = request.query_params.get("site")
    zone_id = request.query_params.get("zone")

    results = []
    for plate, session in latest_by_plate.items():
        vehicle = vehicle_map.get(plate)
        wallet_name = vehicle.wallet.name if vehicle else ""
        site_name = session.site.name or ""
        project_name = session.site.project.name or ""
        zone_name = session.zone.name if session.zone_id else ""

        vehicle_note = (vehicle.note or "").strip() if vehicle else ""
        if search:
            haystack = (
                f"{plate} {wallet_name} {site_name} {project_name} {zone_name} {vehicle_note}"
            ).upper()
            if search not in haystack:
                continue
        if site_id and str(session.site_id) != str(site_id):
            continue
        if zone_id and str(session.zone_id or "") != str(zone_id):
            continue

        results.append(
            {
                "plate": plate,
                "vehicle_id": vehicle.id if vehicle else None,
                "wallet_id": vehicle.wallet_id if vehicle else None,
                "wallet_name": wallet_name or None,
                "is_active": vehicle.is_active if vehicle else None,
                "note": vehicle_note or None,
                "site_id": session.site_id,
                "site_name": site_name,
                "zone_id": session.zone_id,
                "zone_name": zone_name or None,
                "project_id": session.site.project_id,
                "project_name": project_name,
                "is_open": session.end_time is None,
                "last_seen": (session.end_time or session.start_time).isoformat(),
                "session_id": session.id,
                "session_count": session_counts.get(plate, 1),
            }
        )

    results.sort(key=lambda row: (row["is_open"], row["last_seen"]), reverse=True)
    return Response({"count": len(results), "results": results})


class EventViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Event.objects.select_related(
        "device", "device__site", "device__site__project"
    ).all()
    serializer_class = EventSerializer
    permission_classes = [IsStaffUser]
    search_fields = ("plate",)
    ordering_fields = ("created_at",)

    def get_queryset(self):
        qs = apply_project_scope(
            super().get_queryset(),
            self.request.user,
            field="device__site__project_id",
        )
        for param, field in (
            ("device", "device_id"),
            ("site", "device__site_id"),
            ("decision", "decision"),
            ("action", "action"),
        ):
            value = self.request.query_params.get(param)
            if value:
                qs = qs.filter(**{field: value})
        plate = self.request.query_params.get("plate")
        if plate:
            from wallets.services import normalize_plate

            qs = qs.filter(plate=normalize_plate(plate))
        project_id = self.request.query_params.get("project")
        if project_id:
            qs = qs.filter(device__site__project_id=project_id)
        return qs


class SessionViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    queryset = Session.objects.select_related(
        "site",
        "site__project",
        "zone",
        "zone__parent",
        "wallet",
        "entry_device",
        "start_event",
        "start_event__device",
        "start_event__device__site",
        "end_event",
        "end_event__device",
        "end_event__device__site",
    ).all()
    permission_classes = [IsStaffUser]
    search_fields = ("plate",)
    ordering_fields = ("start_time", "end_time")
    http_method_names = ["get", "post", "put", "patch", "head", "options"]

    def get_serializer_class(self):
        if self.action in ("update", "partial_update"):
            return SessionUpdateSerializer
        if self.action == "end":
            return SessionEndSerializer
        if self.action == "retrieve":
            return SessionSerializer
        return SessionListSerializer

    def get_queryset(self):
        qs = apply_project_scope(
            super().get_queryset(), self.request.user, field="site__project_id"
        )
        match_audits = SessionAuditEntry.objects.filter(
            session_id=OuterRef("pk"),
            field__in=EXIT_MATCH_STATE_FIELDS,
        )
        qs = qs.annotate(
            _exit_match_state=Subquery(
                match_audits.order_by("-created_at", "-id").values("field")[:1]
            ),
            _exit_match_ocr=Subquery(
                SessionAuditEntry.objects.filter(
                    session_id=OuterRef("pk"),
                    field=EXIT_MATCH_AUDIT_FIELD,
                )
                .order_by("-created_at", "-id")
                .values("old_value")[:1]
            ),
        )
        if self.action == "retrieve":
            qs = qs.prefetch_related(
                "audit_entries",
                "audit_entries__actor",
                "transactions",
                "transactions__wallet",
                "transactions__wallet__project",
            )
        site_id = self.request.query_params.get("site")
        if site_id:
            qs = qs.filter(site_id=site_id)
        zone_id = self.request.query_params.get("zone")
        if zone_id:
            qs = qs.filter(zone_id=zone_id)
        payment_status = self.request.query_params.get("payment_status")
        if payment_status:
            qs = qs.filter(payment_status=payment_status)
        billing_method = self.request.query_params.get("billing_method")
        if billing_method:
            qs = qs.filter(billing_method=billing_method)
        waiver_kind = self.request.query_params.get("waiver_kind")
        if waiver_kind:
            qs = qs.filter(waiver_kind=waiver_kind)
        billing_exempt = self.request.query_params.get("billing_exempt")
        if billing_exempt is not None and billing_exempt != "":
            truthy = billing_exempt.lower() in ("1", "true", "yes")
            qs = qs.filter(billing_exempt=truthy)
        exit_matched = self.request.query_params.get("exit_matched")
        if exit_matched is not None and exit_matched != "":
            truthy = exit_matched.lower() in ("1", "true", "yes")
            if truthy:
                qs = qs.filter(_exit_match_state=EXIT_MATCH_AUDIT_FIELD)
            else:
                # Include never-matched (NULL) and explicitly undone matches.
                qs = qs.filter(
                    Q(_exit_match_state__isnull=True)
                    | Q(_exit_match_state=EXIT_UNMATCH_AUDIT_FIELD)
                )
        open_only = self.request.query_params.get("open")
        if open_only is not None and open_only.lower() in ("1", "true", "yes"):
            qs = qs.filter(end_time__isnull=True)
        closed_only = self.request.query_params.get("closed")
        if closed_only is not None and closed_only.lower() in ("1", "true", "yes"):
            qs = qs.filter(end_time__isnull=False)
        start_after = self.request.query_params.get("start_after")
        if start_after:
            parsed = parse_query_datetime(start_after)
            if parsed:
                qs = qs.filter(start_time__gte=parsed)
        start_before = self.request.query_params.get("start_before")
        if start_before:
            parsed = parse_query_datetime(start_before, end_of_day=True)
            if parsed:
                qs = qs.filter(start_time__lte=parsed)
        wallet_id = self.request.query_params.get("wallet") or self.request.query_params.get(
            "client"
        )
        if wallet_id:
            # Billing owner at session start (does not follow plate moves).
            qs = qs.filter(wallet_id=wallet_id)
        plate = self.request.query_params.get("plate")
        if plate:
            from wallets.services import normalize_plate

            qs = qs.filter(plate=normalize_plate(plate))
        project_id = self.request.query_params.get("project")
        if project_id:
            qs = qs.filter(site__project_id=project_id)
        return qs

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        session = self.get_object()
        serializer = self.get_serializer(data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if (
            "plate" not in data
            and "billing_exempt" not in data
            and not (data.get("note") or "").strip()
        ):
            return Response(
                {"detail": "Provide plate, billing_exempt, and/or note."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            updated = update_session(
                session,
                user=request.user,
                plate=data.get("plate"),
                billing_exempt=data.get("billing_exempt"),
                note=data.get("note", ""),
            )
        except Exception as exc:
            from rest_framework.exceptions import ValidationError

            if isinstance(exc, ValidationError):
                raise
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(SessionSerializer(updated, context={"request": request}).data)

    def partial_update(self, request, *args, **kwargs):
        kwargs["partial"] = True
        return self.update(request, *args, **kwargs)

    @action(detail=True, methods=["post"])
    def settle(self, request, pk=None):
        """Charge a closed unpaid wallet session (allows negative balance)."""
        session = self.get_object()
        note = ""
        if isinstance(request.data, dict):
            note = (request.data.get("note") or "").strip()
        try:
            settled = settle_session(session, user=request.user, note=note)
        except Exception as exc:
            from rest_framework.exceptions import ValidationError

            if isinstance(exc, ValidationError):
                raise
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(SessionSerializer(settled, context={"request": request}).data)

    @action(
        detail=True,
        methods=["post"],
        url_path="cashier-validate",
        permission_classes=[CanDecideAccessRequest],
    )
    def cashier_validate(self, request, pk=None):
        """
        Desk validate: charge operator wallet, stamp paid-exit grace, keep stay open.
        """
        note = ""
        if isinstance(request.data, dict):
            note = (request.data.get("note") or "").strip()
        try:
            updated = validate_open_session_for_exit(
                self.get_object(), request.user, note=note
            )
        except Exception as exc:
            from rest_framework.exceptions import ValidationError

            if isinstance(exc, ValidationError):
                raise
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(SessionSerializer(updated, context={"request": request}).data)

    @action(
        detail=True,
        methods=["post"],
        url_path="unmatch-exit",
        permission_classes=[CanDecideAccessRequest],
    )
    def unmatch_exit(self, request, pk=None):
        """Undo an active operator exit OCR match; keeps match/unmatch audits."""
        try:
            updated = unmatch_session_exit(self.get_object(), request.user)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(SessionSerializer(updated, context={"request": request}).data)

    @action(detail=True, methods=["get"])
    def bill(self, request, pk=None):
        """Printable bill for a successful pay-at-exit card payment."""
        from parking.session_bill import build_session_bill_html

        session = self.get_object()
        username = getattr(request.user, "username", "") or ""
        try:
            body = build_session_bill_html(session, generated_by=username)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        response = HttpResponse(body, content_type="text/html; charset=utf-8")
        response["Content-Disposition"] = (
            f'attachment; filename="session-{session.pk}-bill.html"'
        )
        return response

    @action(detail=True, methods=["post"])
    def end(self, request, pk=None):
        session = self.get_object()
        serializer = SessionEndSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            closed = end_session(
                session,
                user=request.user,
                note=data.get("note", ""),
                end_time=data.get("end_time"),
            )
        except Exception as exc:
            from rest_framework.exceptions import ValidationError

            if isinstance(exc, ValidationError):
                raise
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(SessionSerializer(closed, context={"request": request}).data)

    @action(detail=False, methods=["get"])
    def export(self, request):
        qs = self.filter_queryset(self.get_queryset()).order_by("-start_time")
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "id",
                "plate",
                "wallet_id",
                "wallet_name",
                "site",
                "zone",
                "project",
                "start_time",
                "end_time",
                "fee",
                "payment_status",
                "billing_method",
                "billing_exempt",
                "waiver_kind",
                "allow_negative_balance",
                "exit_matched",
                "exit_match_ocr",
            ]
        )
        for row in qs.iterator(chunk_size=500):
            writer.writerow(
                [
                    row.id,
                    row.plate,
                    row.wallet_id or "",
                    row.wallet.name if row.wallet_id else "",
                    row.site.name if row.site_id else "",
                    row.zone.name if row.zone_id else "",
                    row.site.project.name if row.site_id else "",
                    row.start_time.isoformat() if row.start_time else "",
                    row.end_time.isoformat() if row.end_time else "",
                    row.fee if row.fee is not None else "",
                    row.payment_status,
                    row.billing_method,
                    "1" if row.billing_exempt else "0",
                    row.waiver_kind or "",
                    "1" if row.allow_negative_balance else "0",
                    "1" if getattr(row, "_exit_match_state", None) == EXIT_MATCH_AUDIT_FIELD else "0",
                    getattr(row, "_exit_match_ocr", "") or "",
                ]
            )
        response = HttpResponse(buffer.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="sessions.csv"'
        return response


class AccessRequestViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = AccessRequest.objects.select_related(
        "device",
        "device__site",
        "device__site__project",
        "device__zone",
        "decided_by",
        "resolution_event",
    ).all()
    serializer_class = AccessRequestSerializer
    permission_classes = [IsStaffUser]
    search_fields = ("plate",)
    ordering_fields = ("created_at", "decided_at")
    ordering = ("-created_at",)

    def get_queryset(self):
        qs = apply_project_scope(
            super().get_queryset(),
            self.request.user,
            field="device__site__project_id",
        )
        status_param = self.request.query_params.get("status")
        decided = self.request.query_params.get("decided")
        if decided is not None and decided.lower() in ("1", "true", "yes"):
            # History: operator decisions only (never pending / timeout).
            qs = qs.filter(
                status__in=(
                    AccessRequest.Status.APPROVED,
                    AccessRequest.Status.DENIED,
                )
            )
            if status_param:
                statuses = [
                    s.strip()
                    for s in status_param.split(",")
                    if s.strip()
                    in (
                        AccessRequest.Status.APPROVED,
                        AccessRequest.Status.DENIED,
                    )
                ]
                if statuses:
                    qs = qs.filter(status__in=statuses)
        elif status_param:
            statuses = [s.strip() for s in status_param.split(",") if s.strip()]
            # Never allow pending/timeout through a bare status filter on history UIs
            # unless explicitly requesting pending (gate control).
            if len(statuses) == 1:
                qs = qs.filter(status=statuses[0])
            elif statuses:
                qs = qs.filter(status__in=statuses)
        else:
            if self.action in ("list", "export"):
                qs = qs.filter(status=AccessRequest.Status.PENDING)
        site_id = self.request.query_params.get("site")
        if site_id:
            qs = qs.filter(device__site_id=site_id)
        zone_id = self.request.query_params.get("zone")
        if zone_id:
            qs = qs.filter(device__zone_id=zone_id)
        device_id = self.request.query_params.get("device")
        if device_id:
            qs = qs.filter(device_id=device_id)
        project_id = self.request.query_params.get("project")
        if project_id:
            qs = qs.filter(device__site__project_id=project_id)
        decided_after = self.request.query_params.get("decided_after")
        if decided_after:
            parsed = parse_query_datetime(decided_after)
            if parsed:
                qs = qs.filter(decided_at__gte=parsed)
        decided_before = self.request.query_params.get("decided_before")
        if decided_before:
            parsed = parse_query_datetime(decided_before, end_of_day=True)
            if parsed:
                qs = qs.filter(decided_at__lte=parsed)
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
                "plate",
                "action",
                "status",
                "site",
                "zone",
                "project",
                "device",
                "system_reason",
                "operator_note",
                "exempted",
                "decided_by",
                "decided_at",
                "created_at",
                "resolution_seconds",
            ]
        )
        for row in qs.iterator(chunk_size=500):
            resolution = ""
            if row.decided_at and row.created_at:
                resolution = int(
                    (row.decided_at - row.created_at).total_seconds()
                )
            writer.writerow(
                [
                    row.id,
                    row.plate,
                    row.action,
                    row.status,
                    row.device.site.name if row.device_id else "",
                    row.device.zone.name
                    if row.device_id and row.device.zone_id
                    else "",
                    row.device.site.project.name if row.device_id else "",
                    str(row.device) if row.device_id else "",
                    row.reason or "",
                    row.decision_note or "",
                    "1" if row.exempted else "0",
                    row.decided_by.username if row.decided_by_id else "",
                    row.decided_at.isoformat() if row.decided_at else "",
                    row.created_at.isoformat() if row.created_at else "",
                    resolution,
                ]
            )
        response = HttpResponse(buffer.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = (
            'attachment; filename="access-requests.csv"'
        )
        return response

    @action(
        detail=False,
        methods=["post"],
        url_path="manual",
        permission_classes=[CanDecideAccessRequest],
    )
    def manual(self, request):
        """
        Enter a plate on an idle gate (gate control). Creates an AccessRequest
        and auto-approves it (opens the gate).
        """
        serializer = ManualPlateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        device_id = serializer.validated_data["device_id"]
        plate = serializer.validated_data["plate"]

        device = (
            apply_project_scope(
                Device.objects.select_related("site", "site__project"),
                request.user,
                field="site__project_id",
            )
            .filter(pk=device_id)
            .first()
        )
        if device is None:
            return Response(
                {"detail": "Device not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            access_request = staff_process_plate(
                device,
                plate,
                request.user,
                note=serializer.validated_data.get("note", ""),
                exempted=serializer.validated_data.get("exempted", False),
                entry_time=serializer.validated_data.get("entry_time"),
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "granted": True,
                "access_request": AccessRequestSerializer(access_request).data,
                "event_id": (
                    access_request.resolution_event_id
                    if access_request.resolution_event_id
                    else None
                ),
            },
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[CanDecideAccessRequest],
    )
    def approve(self, request, pk=None):
        access_request = self.get_object()
        serializer = AccessRequestDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            approve_access_request(
                access_request,
                request.user,
                note=serializer.validated_data.get("note", ""),
                exempted=serializer.validated_data.get("exempted", False),
                entry_time=serializer.validated_data.get("entry_time"),
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        access_request.refresh_from_db()
        return Response(AccessRequestSerializer(access_request).data)

    @action(
        detail=True,
        methods=["post"],
        url_path="validate-payment",
        permission_classes=[CanDecideAccessRequest],
    )
    def validate_payment(self, request, pk=None):
        """Charge operator wallet (negative OK), open gate, start paid-exit grace."""
        access_request = self.get_object()
        serializer = AccessRequestDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            access_request = validate_payment_access_request(
                access_request,
                request.user,
                note=serializer.validated_data.get("note", ""),
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        access_request.refresh_from_db()
        return Response(AccessRequestSerializer(access_request).data)

    @action(
        detail=True,
        methods=["get"],
        url_path="session-matches",
        permission_classes=[CanDecideAccessRequest],
    )
    def session_matches(self, request, pk=None):
        """Open sessions in this exit zone, nearest plate first."""
        access_request = self.get_object()
        return Response(session_matches_for_access_request(access_request))

    @action(
        detail=True,
        methods=["post"],
        url_path="match-session",
        permission_classes=[CanDecideAccessRequest],
    )
    def match_session(self, request, pk=None):
        serializer = AccessRequestSessionMatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            access_request = match_access_request_to_session(
                self.get_object(),
                serializer.validated_data["session_id"],
                request.user,
            )
        except ValueError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        access_request.refresh_from_db()
        return Response(AccessRequestSerializer(access_request).data)

    @action(
        detail=True,
        methods=["post"],
        url_path="unmatch-session",
        permission_classes=[CanDecideAccessRequest],
    )
    def unmatch_session(self, request, pk=None):
        try:
            access_request = unmatch_access_request_from_session(
                self.get_object(),
                request.user,
            )
        except ValueError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        access_request.refresh_from_db()
        return Response(AccessRequestSerializer(access_request).data)

    @action(
        detail=True,
        methods=["post"],
        url_path="cancel-bill",
        permission_classes=[CanDecideAccessRequest],
    )
    def cancel_bill(self, request, pk=None):
        from django.db import transaction

        from payments.models import PaymentIntent
        from payments.services import close_open_payment_intents_for_access_request

        with transaction.atomic():
            access_request = (
                AccessRequest.objects.select_for_update()
                .select_related("device", "device__site", "device__site__project")
                .get(pk=self.get_object().pk)
            )
            if access_request.status != AccessRequest.Status.PENDING:
                return Response(
                    {"detail": "Access request is not pending."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            closed = close_open_payment_intents_for_access_request(
                access_request,
                status=PaymentIntent.Status.FAILED,
            )
            if not closed:
                return Response(
                    {"detail": "No open kiosk bill was found."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            access_request.reason = (
                "Exit payment cancelled by control room — manual decision required"
            )
            access_request.save(update_fields=["reason"])
        return Response(AccessRequestSerializer(access_request).data)

    @action(
        detail=True,
        methods=["post"],
        url_path="send-bill",
        permission_classes=[CanDecideAccessRequest],
    )
    def send_bill(self, request, pk=None):
        """Re-bill the existing open unpaid session to the exit kiosk."""
        from django.db import transaction

        from payments.services import create_payment_intent_for_access_request

        with transaction.atomic():
            access_request = (
                AccessRequest.objects.select_for_update()
                .select_related("device", "device__site", "device__site__project")
                .get(pk=self.get_object().pk)
            )
            try:
                create_payment_intent_for_access_request(access_request)
            except ValueError as exc:
                return Response(
                    {"detail": str(exc)},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            access_request.reason = "Awaiting exit payment"
            access_request.save(update_fields=["reason"])
        access_request.refresh_from_db()
        return Response(AccessRequestSerializer(access_request).data)

    @action(
        detail=True,
        methods=["post"],
        url_path="refresh-bill",
        permission_classes=[CanDecideAccessRequest],
    )
    def refresh_bill(self, request, pk=None):
        """Update the open kiosk bill amount to the current estimated fee."""
        from django.db import transaction

        from payments.services import refresh_payment_intent_amount

        with transaction.atomic():
            access_request = (
                AccessRequest.objects.select_for_update()
                .select_related("device", "device__site", "device__site__project")
                .get(pk=self.get_object().pk)
            )
            try:
                refresh_payment_intent_amount(access_request)
            except ValueError as exc:
                return Response(
                    {"detail": str(exc)},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        access_request.refresh_from_db()
        return Response(AccessRequestSerializer(access_request).data)

    @action(
        detail=True,
        methods=["post"],
        url_path="charge-at-kiosk",
        permission_classes=[CanDecideAccessRequest],
    )
    def charge_at_kiosk(self, request, pk=None):
        from payments.serializers import PaymentIntentSerializer
        from payments.services import create_control_room_payment_intent

        access_request = self.get_object()
        # Unpaid open session must use send-bill / refresh-bill — not Charge.
        from parking.models import Session

        open_session = Session.get_open(
            plate=access_request.plate,
            site_id=access_request.device.site_id,
        )
        if (
            open_session is not None
            and open_session.payment_status != Session.PaymentStatus.PAID
        ):
            return Response(
                {
                    "detail": (
                        "An unpaid open session must be re-billed with "
                        "Resend bill, not Charge at kiosk."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = AccessRequestChargeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            intent = create_control_room_payment_intent(
                access_request,
                mode=serializer.validated_data["mode"],
                entry_time=serializer.validated_data.get("entry_time"),
                actor=request.user,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            PaymentIntentSerializer(intent).data,
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[CanDecideAccessRequest],
    )
    def deny(self, request, pk=None):
        access_request = self.get_object()
        serializer = AccessRequestDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            deny_access_request(
                access_request,
                request.user,
                note=serializer.validated_data.get("note", ""),
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        access_request.refresh_from_db()
        return Response(AccessRequestSerializer(access_request).data)
