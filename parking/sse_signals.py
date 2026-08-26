from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from parking.access_request_bus import access_request_bus
from parking.models import AccessRequest
from parking.serializers import AccessRequestSerializer


@receiver(post_save, sender=AccessRequest)
def publish_access_request_sse(sender, instance: AccessRequest, **kwargs):
    """Notify SSE clients when a request becomes pending or leaves pending."""
    obj = (
        AccessRequest.objects.select_related(
            "device",
            "device__site",
            "device__site__project",
            "device__zone",
            "decided_by",
            "resolution_event",
        )
        .filter(pk=instance.pk)
        .first()
    )
    if obj is None:
        return

    if obj.status == AccessRequest.Status.PENDING:
        data = AccessRequestSerializer(obj).data
        data["site_id"] = obj.device.site_id
        data["project_id"] = obj.device.site.project_id
        data["device_id"] = obj.device_id
        access_request_bus.publish("access_request.pending", data)
    else:
        access_request_bus.publish(
            "access_request.removed",
            {
                "id": obj.pk,
                "status": obj.status,
                "device_id": obj.device_id,
                "site_id": obj.device.site_id,
                "project_id": obj.device.site.project_id,
                "plate": obj.plate,
            },
        )


@receiver(post_delete, sender=AccessRequest)
def publish_access_request_deleted_sse(sender, instance: AccessRequest, **kwargs):
    """Clear waiting cards only when a *pending* AR is deleted (queue swap).

    Historical timeout purges must not fan out Redis PUBLISH per row.
    """
    if instance.status != AccessRequest.Status.PENDING:
        return
    device = getattr(instance, "device", None)
    site = getattr(device, "site", None) if device is not None else None
    project = getattr(site, "project", None) if site is not None else None
    access_request_bus.publish(
        "access_request.removed",
        {
            "id": instance.pk,
            "status": "timeout",
            "device_id": instance.device_id,
            "site_id": getattr(site, "id", None) or getattr(device, "site_id", None),
            "project_id": getattr(project, "id", None),
            "plate": instance.plate,
        },
    )
