from django.urls import path
from rest_framework.routers import DefaultRouter

from .sse import access_request_sse
from .views import (
    AccessRequestViewSet,
    EventViewSet,
    SessionViewSet,
    cashier_me,
    cashier_search,
    ops_dashboard,
    ops_vehicles,
)

router = DefaultRouter()
router.register("events", EventViewSet, basename="event")
router.register("sessions", SessionViewSet, basename="session")
router.register("access-requests", AccessRequestViewSet, basename="access-request")

urlpatterns = [
    path("ops/dashboard/", ops_dashboard, name="ops-dashboard"),
    path("ops/vehicles/", ops_vehicles, name="ops-vehicles"),
    path("cashier/me/", cashier_me, name="cashier-me"),
    path("cashier/search/", cashier_search, name="cashier-search"),
    # Must be before router.urls — otherwise DRF treats "stream" as pk and 406s
    # on Accept: text/event-stream (EventSource).
    path(
        "access-requests/stream/",
        access_request_sse,
        name="access-request-sse",
    ),
    *router.urls,
]
