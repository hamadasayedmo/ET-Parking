from django.core.management.base import BaseCommand

from parking.mqtt import mqtt_configured, publish_site_device_configs
from sites.models import Site


class Command(BaseCommand):
    help = "Publish retained MQTT config for all devices (optionally one site)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--site-id",
            type=int,
            default=None,
            help="Only publish devices for this site id",
        )

    def handle(self, *args, **options):
        if not mqtt_configured():
            self.stderr.write("MQTT is not configured.")
            return

        site_id = options["site_id"]
        sites = Site.objects.filter(pk=site_id) if site_id else Site.objects.all()
        if not sites.exists():
            self.stderr.write("No matching sites.")
            return

        total_ok = total_fail = 0
        for site in sites:
            ok, fail = publish_site_device_configs(site.pk)
            total_ok += ok
            total_fail += fail
            self.stdout.write(
                f"Site {site.pk} ({site.name}): published={ok} failed={fail}"
            )

        self.stdout.write(self.style.SUCCESS(f"Done. ok={total_ok} fail={total_fail}"))
