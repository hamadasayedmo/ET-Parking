from django.urls import include, path

urlpatterns = [
    path("auth/", include("accounts.urls")),
    path("projects/", include("projects.urls")),
    path("payments/", include("payments.urls")),
    path("", include("sites.urls")),
    path("", include("wallets.urls")),
    path("", include("parking.urls")),
]
