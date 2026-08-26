from django.db.models import Count, Q
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.permissions import IsStaffUser
from accounts.scoping import apply_project_scope
from parking.models import Session
from sites.models import Device
from wallets.models import Vehicle

from .models import Project
from .serializers import ProjectSerializer


class ProjectViewSet(viewsets.ModelViewSet):
    queryset = Project.objects.all()
    serializer_class = ProjectSerializer
    permission_classes = [IsStaffUser]
    search_fields = ("name",)
    ordering_fields = ("name", "created_at")

    def get_queryset(self):
        qs = Project.objects.annotate(
            site_count_agg=Count(
                "sites",
                filter=Q(sites__is_active=True),
                distinct=True,
            )
        )
        return apply_project_scope(qs, self.request.user, field="id")

    @action(detail=True, methods=["get"])
    def dashboard(self, request, pk=None):
        """Aggregated ops snapshot for a single project."""
        project = self.get_object()
        sites_qs = project.sites.filter(is_active=True)
        devices_qs = Device.objects.filter(
            site__project=project, site__is_active=True
        )
        sessions_qs = Session.objects.filter(
            site__project=project, site__is_active=True
        )

        plates = list(sessions_qs.values_list("plate", flat=True).distinct())
        vehicles_qs = (
            Vehicle.objects.filter(plate__in=plates) if plates else Vehicle.objects.none()
        )
        wallet_ids = vehicles_qs.values_list("wallet_id", flat=True).distinct()

        return Response(
            {
                "project": ProjectSerializer(project).data,
                "stats": {
                    "sites": sites_qs.count(),
                    "devices": devices_qs.count(),
                    "devices_enabled": devices_qs.filter(enabled=True).count(),
                    "sessions": sessions_qs.count(),
                    "sessions_open": sessions_qs.filter(end_time__isnull=True).count(),
                    "vehicles": len(plates),
                    "wallets": wallet_ids.count() if plates else 0,
                },
            }
        )

    @action(detail=True, methods=["get"])
    def vehicles(self, request, pk=None):
        """Plates seen at this project's sites, with latest site context."""
        project = self.get_object()
        sessions = (
            Session.objects.filter(site__project=project)
            .select_related("site", "zone")
            .order_by("-start_time")
        )

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
            zone_name = session.zone.name if session.zone_id else ""

            if search:
                haystack = f"{plate} {wallet_name} {site_name} {zone_name}".upper()
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
                    "site_id": session.site_id,
                    "site_name": site_name,
                    "zone_id": session.zone_id,
                    "zone_name": zone_name or None,
                    "is_open": session.end_time is None,
                    "last_seen": (session.end_time or session.start_time).isoformat(),
                    "session_id": session.id,
                    "session_count": session_counts.get(plate, 1),
                }
            )

        # Open stays first, then most recently seen.
        results.sort(key=lambda row: (row["is_open"], row["last_seen"]), reverse=True)

        return Response({"count": len(results), "results": results})
