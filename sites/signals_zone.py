from django.db.models.signals import post_save
from django.dispatch import receiver

from sites.models import Site, Zone


@receiver(post_save, sender=Site)
def ensure_default_zone(sender, instance, created, **kwargs):
    if not created:
        return
    Zone.objects.get_or_create(
        site=instance,
        name="Main",
        defaults={
            "hourly_rate": instance.hourly_rate,
            "additional_fee": instance.additional_fee,
            "is_active": True,
        },
    )
