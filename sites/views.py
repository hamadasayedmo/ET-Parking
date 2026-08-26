from django.db.models import Count, Q, Sum
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from accounts.permissions import (
    CanDecideAccessRequest,
    IsStaffReadSuperuserWrite,
    IsStaffUser,
)
from accounts.scoping import apply_project_scope, user_can_access_project
from parking.models import AccessRequest, Session
from parking.mqtt import publish_once
from parking.serializers import AccessRequestSerializer, SessionListSerializer

from .models import Device, Site, Zone
from .serializers import DeviceSerializer, SiteSerializer, ZoneSerializer


class FlexiblePagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 500


def send_device_command(device: Device, command: str) -> tuple[bool, str]:
    """Publish a barrier command and update local lock state when relevant."""
    try:
        payload = device.get_command_payload(command)
    except ValueError as exc:
        return False, str(exc)

    ok = publish_once(device.get_command_topic(), payload)
    if not ok:
        return False, "Failed to publish MQTT command (check IoT configuration)."

    if command == "lock":
        device.gate_locked = True
        device.save(update_fields=["gate_locked", "updated_at"])
    elif command == "unlock":
        device.gate_locked = False
        device.save(update_fields=["gate_locked", "updated_at"])

    return True, f"Command '{command}' sent."


class SiteViewSet(viewsets.ModelViewSet):
    queryset = Site.objects.select_related("project").all()
    serializer_class = SiteSerializer
    permission_classes = [IsStaffUser]
    pagination_class = FlexiblePagination
    search_fields = ("name", "project__name")
    ordering_fields = ("name", "hourly_rate")

    def get_queryset(self):
        qs = apply_project_scope(super().get_queryset(), self.request.user)
        qs = qs.annotate(
            entry_device_count=Count(
                "devices",
                filter=Q(devices__type=Device.Type.ENTRY),
                distinct=True,
            ),
            exit_device_count=Count(
                "devices",
                filter=Q(devices__type=Device.Type.EXIT),
                distinct=True,
            ),
            annotated_device_count=Count("devices", distinct=True),
            annotated_zone_count=Count("zones", distinct=True),
        )
        project_id = self.request.query_params.get("project")
        if project_id:
            qs = qs.filter(project_id=project_id)
        is_active = self.request.query_params.get("is_active")
        if is_active is not None and is_active != "":
            qs = qs.filter(is_active=is_active.lower() in ("1", "true", "yes"))
        else:
            # Admin UI only works with live sites.
            qs = qs.filter(is_active=True)
        return qs

    def perform_create(self, serializer):
        project = serializer.validated_data["project"]
        if not user_can_access_project(self.request.user, project.pk):
            raise PermissionDenied("You cannot create sites in this project.")
        serializer.save()

    def perform_update(self, serializer):
        project = serializer.validated_data.get("project", self.get_object().project)
        if not user_can_access_project(self.request.user, project.pk):
            raise PermissionDenied("You cannot move sites into this project.")
        serializer.save()

    @action(detail=True, methods=["get"])
    def dashboard(self, request, pk=None):
        site = self.get_object()
        today_start = timezone.localtime(timezone.now()).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        devices = Device.objects.filter(site=site).order_by("type", "id")
        sessions = Session.objects.filter(site=site).select_related(
            "site", "site__project"
        )
        open_sessions = sessions.filter(end_time__isnull=True)[:20]
        sessions_today = sessions.filter(start_time__gte=today_start)
        pending = AccessRequest.objects.filter(
            status=AccessRequest.Status.PENDING,
            device__site=site,
        ).select_related(
            "device",
            "device__site",
            "device__site__project",
            "decided_by",
            "resolution_event",
        )[:20]
        revenue_today = (
            sessions_today.filter(payment_status=Session.PaymentStatus.PAID).aggregate(
                total=Sum("fee")
            )["total"]
            or 0
        )

        return Response(
            {
                "site": SiteSerializer(site).data,
                "stats": {
                    "devices": devices.count(),
                    "devices_enabled": devices.filter(enabled=True).count(),
                    "devices_locked": devices.filter(gate_locked=True).count(),
                    "sessions_open": sessions.filter(end_time__isnull=True).count(),
                    "sessions_today": sessions_today.count(),
                    "pending_requests": AccessRequest.objects.filter(
                        status=AccessRequest.Status.PENDING,
                        device__site=site,
                    ).count(),
                    "revenue_today": f"{revenue_today:.2f}",
                    "revenue_currency": "SAR",
                },
                "devices": DeviceSerializer(devices, many=True).data,
                "open_sessions": SessionListSerializer(open_sessions, many=True).data,
                "pending_requests": AccessRequestSerializer(pending, many=True).data,
            }
        )


class ZoneViewSet(viewsets.ModelViewSet):
    queryset = Zone.objects.select_related("site", "site__project", "parent").all()
    serializer_class = ZoneSerializer
    permission_classes = [IsStaffReadSuperuserWrite]
    pagination_class = FlexiblePagination
    search_fields = ("name", "site__name")
    ordering_fields = ("name", "hourly_rate")

    def get_queryset(self):
        qs = apply_project_scope(
            super().get_queryset(), self.request.user, field="site__project_id"
        )
        qs = qs.annotate(annotated_device_count=Count("devices", distinct=True))
        site_id = self.request.query_params.get("site")
        if site_id:
            qs = qs.filter(site_id=site_id)
        project_id = self.request.query_params.get("project")
        if project_id:
            qs = qs.filter(site__project_id=project_id)
        parent = self.request.query_params.get("parent")
        if parent == "null":
            qs = qs.filter(parent__isnull=True)
        elif parent:
            qs = qs.filter(parent_id=parent)
        is_active = self.request.query_params.get("is_active")
        if is_active is not None and is_active != "":
            qs = qs.filter(is_active=is_active.lower() in ("1", "true", "yes"))
        return qs

    def perform_create(self, serializer):
        site = serializer.validated_data["site"]
        if not user_can_access_project(self.request.user, site.project_id):
            raise PermissionDenied("You cannot create zones in this project.")
        serializer.save()

    def perform_update(self, serializer):
        site = serializer.validated_data.get("site", self.get_object().site)
        if not user_can_access_project(self.request.user, site.project_id):
            raise PermissionDenied("You cannot move zones into this project.")
        serializer.save()


class DeviceViewSet(viewsets.ModelViewSet):
    queryset = Device.objects.select_related(
        "site", "site__project", "zone", "zone__parent", "kiosk_credential"
    ).all()
    serializer_class = DeviceSerializer
    permission_classes = [IsStaffUser]
    pagination_class = FlexiblePagination
    search_fields = ("name", "ip", "username", "site__name", "zone__name")
    ordering_fields = ("id", "name", "created_at", "type")

    def get_queryset(self):
        qs = apply_project_scope(
            super().get_queryset(), self.request.user, field="site__project_id"
        )
        site_id = self.request.query_params.get("site")
        if site_id:
            qs = qs.filter(site_id=site_id)
        zone_id = self.request.query_params.get("zone")
        if zone_id:
            qs = qs.filter(zone_id=zone_id)
        project_id = self.request.query_params.get("project")
        if project_id:
            qs = qs.filter(site__project_id=project_id)
        device_type = self.request.query_params.get("type")
        if device_type:
            qs = qs.filter(type=device_type)
        enabled = self.request.query_params.get("enabled")
        if enabled is not None:
            qs = qs.filter(enabled=enabled.lower() in ("1", "true", "yes"))
        site_active = self.request.query_params.get("site_active")
        if site_active is not None:
            qs = qs.filter(
                site__is_active=site_active.lower() in ("1", "true", "yes")
            )
        return qs

    def perform_create(self, serializer):
        zone = serializer.validated_data["zone"]
        if not user_can_access_project(self.request.user, zone.site.project_id):
            raise PermissionDenied("You cannot create devices in this project.")
        serializer.save()

    def perform_update(self, serializer):
        zone = serializer.validated_data.get("zone", self.get_object().zone)
        if not user_can_access_project(self.request.user, zone.site.project_id):
            raise PermissionDenied("You cannot move devices into this project.")
        serializer.save()

    def _run_command(self, request, command: str):
        device = self.get_object()
        if not device.enabled and command != "unlock":
            return Response(
                {"detail": "Device is disabled."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        ok, message = send_device_command(device, command)
        device.refresh_from_db()
        if not ok:
            return Response({"detail": message}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            {
                "detail": message,
                "command": command,
                "device": DeviceSerializer(device).data,
            }
        )

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[CanDecideAccessRequest],
    )
    def pulse(self, request, pk=None):
        return self._run_command(request, "pulse")

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[CanDecideAccessRequest],
    )
    def open(self, request, pk=None):
        return self._run_command(request, "open")

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[CanDecideAccessRequest],
    )
    def close(self, request, pk=None):
        return self._run_command(request, "close")

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[CanDecideAccessRequest],
    )
    def lock(self, request, pk=None):
        return self._run_command(request, "lock")

    @action(
        detail=True,
        methods=["post"],
        permission_classes=[CanDecideAccessRequest],
    )
    def unlock(self, request, pk=None):
        return self._run_command(request, "unlock")
