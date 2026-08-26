from django.db.models.signals import post_save
from django.dispatch import receiver

from parking.models import AccessRequest, Event


@receiver(post_save, sender=AccessRequest)
def create_pay_at_exit_intent_on_pending(
    sender,
    instance: AccessRequest,
    created: bool,
    **kwargs,
):
    """When exit is held under pay-at-exit, create a bill for the exit kiosk."""
    if not created:
        return
    if instance.status != AccessRequest.Status.PENDING:
        return
    if instance.action != Event.Action.EXIT:
        return

    project = instance.device.site.project
    if not project.pay_at_exit_enabled:
        return

    # Gate-control manual opens are auto-approved; do not bill.
    if instance.reason == "Manual entry from gate control":
        return

    from parking.services import device_has_active_kiosk
    from payments.services import create_pay_at_exit_intent

    # No paired kiosk → AR only (ops decides manually); never create a ghost bill.
    if not device_has_active_kiosk(instance.device):
        return

    create_pay_at_exit_intent(instance)
