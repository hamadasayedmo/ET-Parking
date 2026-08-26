from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .sse import kiosk_payment_sse
from .views import (
    KioskCredentialViewSet,
    KioskMeView,
    KioskPairView,
    KioskPaymentCompleteView,
    KioskPendingPaymentsView,
    NearPayJwtView,
    PaymentIntentViewSet,
    PublicReceiptView,
    StaffMintKioskPairingCodeView,
)

router = DefaultRouter()
router.register("intents", PaymentIntentViewSet, basename="payment-intent")
router.register(
    "kiosk-credentials",
    KioskCredentialViewSet,
    basename="kiosk-credential",
)

urlpatterns = [
    path(
        "receipts/",
        PublicReceiptView.as_view(),
        name="payment-receipts",
    ),
    path(
        "nearpay/jwt/",
        NearPayJwtView.as_view(),
        name="nearpay-jwt",
    ),
    path(
        "kiosk/pairing-codes/",
        StaffMintKioskPairingCodeView.as_view(),
        name="kiosk-pairing-code-mint",
    ),
    path("kiosk/pair/", KioskPairView.as_view(), name="kiosk-pair"),
    path("kiosk/me/", KioskMeView.as_view(), name="kiosk-me"),
    path(
        "kiosk/pending/",
        KioskPendingPaymentsView.as_view(),
        name="kiosk-payments-pending",
    ),
    path(
        "kiosk/payments/<int:pk>/complete/",
        KioskPaymentCompleteView.as_view(),
        name="kiosk-payment-complete",
    ),
    path("kiosk/events/", kiosk_payment_sse, name="kiosk-payment-events"),
    path("", include(router.urls)),
]
