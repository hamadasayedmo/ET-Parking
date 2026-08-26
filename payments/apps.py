from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "payments"
    verbose_name = "Payments"

    def ready(self):
        from . import signals  # noqa: F401
        from . import sse_signals  # noqa: F401
