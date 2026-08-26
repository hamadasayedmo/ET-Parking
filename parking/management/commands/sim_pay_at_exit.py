"""
Simulate entry + exit hold so the payment kiosk receives a pending bill.

Usage:
  python manage.py sim_pay_at_exit --plate TEST001
  python manage.py sim_pay_at_exit --plate TEST001 --hours 2 --exit-device-id 5
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from parking.models import AccessRequest, Session
from parking.services import authorize_plate
from payments.models import PaymentIntent
from sites.models import Device


class Command(BaseCommand):
    help = (
        "Open a pay-at-exit card session (entry), then trigger exit hold "
        "so the kiosk SSE shows a payment bill."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--plate",
            default="TEST001",
            help="Plate to simulate (default: TEST001)",
        )
        parser.add_argument(
            "--site-id",
            type=int,
            default=None,
            help="Site id (default: first site that has entry+exit devices)",
        )
        parser.add_argument(
            "--entry-device-id",
            type=int,
            default=None,
            help="Entry device id (optional)",
        )
        parser.add_argument(
            "--exit-device-id",
            type=int,
            default=None,
            help="Exit device id — should be the kiosk-bound exit (optional)",
        )
        parser.add_argument(
            "--hours",
            type=float,
            default=2.0,
            help="Backdate session start by this many hours so fee > 0 (default: 2)",
        )
        parser.add_argument(
            "--entry-only",
            action="store_true",
            help="Only open the session; do not trigger exit",
        )
        parser.add_argument(
            "--exit-only",
            action="store_true",
            help="Only trigger exit for an already-open session",
        )

    def handle(self, *args, **options):
        plate = "".join(str(options["plate"]).split()).upper()
        if not plate:
            raise CommandError("--plate is required")

        entry, exit_device = self._resolve_devices(options)
        project = entry.site.project

        if not project.pay_at_exit_enabled:
            project.pay_at_exit_enabled = True
            project.save(update_fields=["pay_at_exit_enabled"])
            self.stdout.write(
                self.style.WARNING(
                    f"Enabled pay_at_exit on project {project.name!r} ({project.pk})"
                )
            )

        hours = float(options["hours"])
        if hours < 0:
            raise CommandError("--hours must be >= 0")

        if not options["exit_only"]:
            self._clear_plate_state(plate, entry.site_id)
            self.stdout.write(
                f"ENTRY  device={entry.pk} ({entry.name}) site={entry.site_id} "
                f"plate={plate}"
            )
            entry_result = authorize_plate(entry, plate)
            if not entry_result.granted:
                raise CommandError(
                    "Entry was not granted — "
                    f"AR={getattr(entry_result.access_request, 'pk', None)} "
                    f"reason={getattr(entry_result.access_request, 'reason', None)}"
                )

            session = Session.get_open(plate=plate, site_id=entry.site_id)
            if session is None:
                raise CommandError("Entry granted but no open session found")

            if hours > 0:
                session.start_time = timezone.now() - timedelta(hours=hours)
                session.save(update_fields=["start_time"])

            fee = session.estimated_fee()
            currency = getattr(project, "currency", None) or "SAR"
            self.stdout.write(
                self.style.SUCCESS(
                    f"  session={session.pk} billing={session.billing_method} "
                    f"start={session.start_time.isoformat()} fee≈{fee} {currency}"
                )
            )
            if fee <= Decimal("0.00"):
                self.stdout.write(
                    self.style.WARNING(
                        "  Fee is 0 — increase --hours or site hourly_rate / lower grace"
                    )
                )

        if options["entry_only"]:
            self.stdout.write(self.style.SUCCESS("Done (entry-only)."))
            return

        session = Session.get_open(plate=plate, site_id=exit_device.site_id)
        if session is None:
            raise CommandError(
                f"No open session for {plate} at site {exit_device.site_id}. "
                "Run without --exit-only first."
            )

        self.stdout.write(
            f"EXIT   device={exit_device.pk} ({exit_device.name}) site={exit_device.site_id} "
            f"plate={plate}"
        )
        exit_result = authorize_plate(exit_device, plate)
        if exit_result.granted:
            raise CommandError(
                "Exit was auto-granted (no payment hold). "
                "Check pay_at_exit_enabled and that the plate is not wallet/exempt."
            )

        ar = exit_result.access_request
        if ar is None or ar.status != AccessRequest.Status.PENDING:
            raise CommandError(
                f"Expected pending AccessRequest, got {ar}"
            )

        intent = (
            PaymentIntent.objects.filter(access_request=ar)
            .order_by("-created_at")
            .first()
        )
        if intent is None:
            raise CommandError(
                f"Pending AR={ar.pk} but no PaymentIntent — check payments signals"
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"  access_request={ar.pk} status={ar.status}\n"
                f"  payment_intent={intent.pk} amount={intent.amount} "
                f"{intent.currency} status={intent.status}\n"
                f"  merchant_ref={intent.merchant_reference_id}"
            )
        )
        self.stdout.write(
            self.style.NOTICE(
                "Kiosk should show the bill via SSE. Complete payment on the tablet."
            )
        )

    def _resolve_devices(self, options) -> tuple[Device, Device]:
        entry_id = options.get("entry_device_id")
        exit_id = options.get("exit_device_id")
        site_id = options.get("site_id")

        if entry_id and exit_id:
            entry = Device.objects.select_related("site", "site__project").filter(
                pk=entry_id, type=Device.Type.ENTRY
            ).first()
            exit_device = Device.objects.select_related("site", "site__project").filter(
                pk=exit_id, type=Device.Type.EXIT
            ).first()
            if entry is None:
                raise CommandError(f"Entry device {entry_id} not found")
            if exit_device is None:
                raise CommandError(f"Exit device {exit_id} not found")
            if entry.site_id != exit_device.site_id:
                raise CommandError("Entry and exit must be on the same site")
            return entry, exit_device

        qs = Device.objects.select_related("site", "site__project").filter(
            enabled=True, site__is_active=True
        )
        if site_id:
            qs = qs.filter(site_id=site_id)

        if entry_id:
            entry = qs.filter(pk=entry_id, type=Device.Type.ENTRY).first()
            if entry is None:
                raise CommandError(f"Entry device {entry_id} not found")
            exit_device = qs.filter(
                site_id=entry.site_id, type=Device.Type.EXIT
            ).order_by("id").first()
            if exit_device is None:
                raise CommandError(f"No exit device on site {entry.site_id}")
            return entry, exit_device

        if exit_id:
            exit_device = qs.filter(pk=exit_id, type=Device.Type.EXIT).first()
            if exit_device is None:
                raise CommandError(f"Exit device {exit_id} not found")
            entry = qs.filter(
                site_id=exit_device.site_id, type=Device.Type.ENTRY
            ).order_by("id").first()
            if entry is None:
                raise CommandError(f"No entry device on site {exit_device.site_id}")
            return entry, exit_device

        # First site that has both entry and exit
        for site_pk in (
            qs.filter(type=Device.Type.ENTRY)
            .values_list("site_id", flat=True)
            .distinct()
        ):
            entry = qs.filter(site_id=site_pk, type=Device.Type.ENTRY).order_by("id").first()
            exit_device = (
                qs.filter(site_id=site_pk, type=Device.Type.EXIT).order_by("id").first()
            )
            if entry and exit_device:
                return entry, exit_device

        raise CommandError(
            "No site with both an entry and exit device. "
            "Pass --entry-device-id / --exit-device-id / --site-id."
        )

    def _clear_plate_state(self, plate: str, site_id: int) -> None:
        """Drop stale pending ARs / open sessions for a clean sim."""
        pending = AccessRequest.objects.filter(
            plate=plate,
            device__site_id=site_id,
            status=AccessRequest.Status.PENDING,
        )
        n_ar = pending.count()
        if n_ar:
            pending.delete()
            self.stdout.write(f"  cleared {n_ar} pending AccessRequest(s)")

        open_sessions = Session.objects.filter(
            plate=plate, site_id=site_id, end_time__isnull=True
        )
        for session in open_sessions:
            session.end_time = timezone.now()
            session.save(update_fields=["end_time"])
            self.stdout.write(f"  closed leftover session={session.pk}")
