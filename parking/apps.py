from django.apps import AppConfig


class ParkingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "parking"

    def ready(self):
        import parking.signals  # noqa: F401
        import parking.sse_signals  # noqa: F401
        # Only the web/SSE process needs to SUBSCRIBE. mqtt_worker only PUBLISHes.
        import sys

        if "mqtt_worker" in sys.argv:
            return
        try:
            from parking.access_request_bus import access_request_bus

            access_request_bus.ensure_redis_listener()
        except Exception:
            pass
