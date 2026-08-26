from django.db.models.signals import post_save
from django.dispatch import receiver

from payments.intent_bus import payment_intent_bus
from payments.models import PaymentIntent
from payments.serializers import PaymentIntentSerializer


OPEN = (PaymentIntent.Status.CREATED, PaymentIntent.Status.PENDING)


def _publish_intent(instance: PaymentIntent) -> None:
    intent = (
        PaymentIntent.objects.select_related(
            "session",
            "session__site",
            "project",
            "access_request",
            "access_request__device",
        )
        .filter(pk=instance.pk)
        .first()
    )
    if intent is None or intent.access_request_id is None:
        return

    device = intent.access_request.device
    if intent.status in OPEN:
        data = dict(PaymentIntentSerializer(intent).data)
        data["device_id"] = device.pk
        data["project_id"] = intent.project_id
        payment_intent_bus.publish("payment_intent.pending", data)
    else:
        payment_intent_bus.publish(
            "payment_intent.removed",
            {
                "id": intent.pk,
                "status": intent.status,
                "device_id": device.pk,
                "project_id": intent.project_id,
                "plate": intent.session.plate,
            },
        )


@receiver(post_save, sender=PaymentIntent)
def publish_payment_intent_sse(sender, instance: PaymentIntent, **kwargs):
    _publish_intent(instance)
