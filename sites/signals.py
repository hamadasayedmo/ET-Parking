import logging

from django.db.models.signals import post_delete, post_save
from django.dispatch import Signal, receiver

from parking.mqtt import publish_once

logger = logging.getLogger(__name__)

site_config_changed = Signal()


@receiver(post_save, sender="sites.Device")
def publish_device_config(sender, instance, **kwargs):
    """Push camera/barrier config to the edge gateway when a Device is saved."""
    topic = instance.get_config_topic()
    payload = instance.to_mqtt_config()
    ok = publish_once(topic, payload)
    if ok:
        logger.info("Published device config to %s", topic)
        site_config_changed.send(sender=sender, device=instance)
    else:
        logger.warning("Device config not published for device_id=%s", instance.pk)


@receiver(post_delete, sender="sites.Device")
def unpublish_device_config(sender, instance, **kwargs):
    """Tell the edge gateway to drop a deleted device."""
    topic = instance.get_config_topic()
    payload = {
        "camera_id": instance.pk,
        "enabled": False,
        "action": "remove",
    }
    ok = publish_once(topic, payload)
    if ok:
        logger.info("Published device remove to %s", topic)
    else:
        logger.warning("Device remove not published for device_id=%s", instance.pk)
