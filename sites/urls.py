from rest_framework.routers import DefaultRouter

from .views import DeviceViewSet, SiteViewSet, ZoneViewSet

router = DefaultRouter()
router.register("sites", SiteViewSet, basename="site")
router.register("zones", ZoneViewSet, basename="zone")
router.register("devices", DeviceViewSet, basename="device")

urlpatterns = router.urls
