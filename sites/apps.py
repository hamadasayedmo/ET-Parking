from django.apps import AppConfig


class SitesConfig(AppConfig):
    name = "sites"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import signals  # noqa: F401
        from . import signals_zone  # noqa: F401
