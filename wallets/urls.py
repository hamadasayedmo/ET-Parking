from rest_framework.routers import DefaultRouter

from .views import TransactionViewSet, VehicleViewSet, WalletViewSet

router = DefaultRouter()
router.register("vehicles", VehicleViewSet, basename="vehicle")
router.register("wallets", WalletViewSet, basename="wallet")
router.register("transactions", TransactionViewSet, basename="transaction")

urlpatterns = router.urls
