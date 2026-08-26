from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models


class Site(models.Model):
    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.PROTECT,
        related_name="sites",
    )
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(
        default=True,
        help_text="Inactive sites ignore camera events and do not open gates.",
    )
    hourly_rate = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("2.00"),
        help_text="Default rate copied onto new zones (live billing uses zone rates).",
    )
    additional_fee = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("1.00"),
        help_text="Default flat fee copied onto new zones.",
    )
    grace_minutes = models.PositiveIntegerField(
        default=0,
        help_text=(
            "Free exit if the visit ends within this many minutes of entry; "
            "also free outer exits after an inner stack settlement. "
            "Defaults from the project when the site is created."
        ),
    )
    iot_thing_name = models.CharField(max_length=128, blank=True, default="")
    iot_certificate_id = models.CharField(max_length=128, blank=True, default="")
    provisioned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.project.name})"

    @property
    def default_iot_thing_name(self) -> str:
        return f"site-{self.pk}-gateway"

    def minimum_entry_balance(self) -> Decimal:
        """Legacy helper — prefer Zone.minimum_entry_balance() for live checks."""
        return self.hourly_rate + self.additional_fee

    def effective_grace_minutes(self) -> int:
        """Grace used for billing at this site (falls back to project default)."""
        if self.grace_minutes:
            return int(self.grace_minutes)
        return int(getattr(self.project, "grace_minutes", 0) or 0)

    def save(self, *args, **kwargs):
        # New sites inherit project grace when left at 0 and project has a default.
        if not self.pk and not self.grace_minutes and self.project_id:
            self.grace_minutes = int(
                getattr(self.project, "grace_minutes", 0) or 0
            )
        super().save(*args, **kwargs)


class Zone(models.Model):
    """Parking zone under a site; may nest inside a parent zone."""

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="zones")
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="children",
        help_text="Outer zone that contains this one (optional).",
    )
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)
    hourly_rate = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("2.00"),
        help_text="Billed per started hour (ceil, minimum 1 hour).",
    )
    additional_fee = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("1.00"),
        help_text="Flat fee added when this zone's session is billed.",
    )

    class Meta:
        ordering = ["site_id", "name"]
        indexes = [
            models.Index(fields=["site", "is_active"]),
            models.Index(fields=["parent"]),
        ]

    def __str__(self):
        return f"{self.name} @ {self.site.name}"

    def minimum_entry_balance(self) -> Decimal:
        return self.hourly_rate + self.additional_fee

    def clean(self):
        super().clean()
        if self.parent_id:
            if self.parent.site_id != self.site_id:
                raise ValidationError(
                    {"parent": "Parent zone must belong to the same site."}
                )
            # Prevent cycles.
            seen = {self.pk} if self.pk else set()
            node = self.parent
            while node is not None:
                if node.pk in seen:
                    raise ValidationError({"parent": "Zone parent would create a cycle."})
                seen.add(node.pk)
                node = node.parent

    def ancestor_chain(self) -> list["Zone"]:
        """This zone followed by parents outward (inner → outer)."""
        chain = [self]
        node = self.parent
        seen = {self.pk} if self.pk else set()
        while node is not None:
            if node.pk in seen:
                break
            seen.add(node.pk)
            chain.append(node)
            node = node.parent
        return chain


class Device(models.Model):
    class Type(models.TextChoices):
        ENTRY = "entry", "Entry"
        EXIT = "exit", "Exit"

    zone = models.ForeignKey(
        Zone,
        on_delete=models.PROTECT,
        related_name="devices",
    )
    # Denormalized from zone.site for MQTT topics and existing ORM filters.
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="devices")
    name = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Optional label (e.g. Entry North).",
    )
    ip = models.GenericIPAddressField()
    username = models.CharField(max_length=64)
    password = models.CharField(max_length=128)
    type = models.CharField(
        max_length=8,
        choices=Type.choices,
        default=Type.ENTRY,
    )
    barrier_lane = models.PositiveSmallIntegerField(default=1)
    enabled = models.BooleanField(default=True)
    gate_locked = models.BooleanField(
        default=False,
        help_text="When locked, edge should ignore auto-open until unlocked.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    GATE_COMMANDS = frozenset({"pulse", "open", "close", "lock", "unlock"})

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["site", "enabled"]),
            models.Index(fields=["site", "type"]),
            models.Index(fields=["zone", "enabled"]),
            models.Index(fields=["zone", "type"]),
        ]

    def __str__(self):
        label = self.name.strip() or f"Device {self.id}"
        return f"{label} ({self.type}) @ {self.ip}"

    def save(self, *args, **kwargs):
        if not self.zone_id and self.site_id:
            default = (
                Zone.objects.filter(site_id=self.site_id, name="Main").first()
                or Zone.objects.filter(site_id=self.site_id, name="Default").first()
                or Zone.objects.filter(site_id=self.site_id).first()
            )
            if default is None:
                default = Zone.objects.create(
                    site_id=self.site_id,
                    name="Main",
                    hourly_rate=self.site.hourly_rate,
                    additional_fee=self.site.additional_fee,
                )
            self.zone = default
        if self.zone_id:
            self.site_id = self.zone.site_id
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        if self.zone_id and self.site_id and self.zone.site_id != self.site_id:
            raise ValidationError({"zone": "Zone must belong to the device site."})

    def get_events_topic(self) -> str:
        return f"edge/{self.site_id}/cameras/{self.id}/events"

    def get_command_topic(self) -> str:
        return f"edge/{self.site_id}/cameras/{self.id}/command"

    def get_config_topic(self) -> str:
        return f"edge/{self.site_id}/cameras/{self.id}/config"

    def to_mqtt_config(self) -> dict:
        """Payload published to edge/{site_id}/cameras/{device_id}/config"""
        return {
            "camera_id": self.id,
            "ip": self.ip,
            "username": self.username,
            "password": self.password,
            "type": self.type,
            "listen_path": f"/events/{self.id}",
            "enabled": self.enabled,
            "actions": {
                "on_plate": "log",
                "auto_open": False,
                "barrier_mode": "pulse",
                "pulse_seconds": 1.0,
                "barrier_lane": self.barrier_lane,
                "configure_platform_control": True,
                "sira_protocol_enabled": False,
            },
        }

    def get_pulse_payload(self) -> dict:
        """Isolates the physical hardware payload schema."""
        return self.get_command_payload("pulse")

    def get_command_payload(self, command: str) -> dict:
        """MQTT command payload for edge/{site}/cameras/{device}/command."""
        command = (command or "").strip().lower()
        if command not in self.GATE_COMMANDS:
            raise ValueError(f"Unsupported gate command: {command}")
        return {
            "command": command,
            "camera_id": self.id,
            "barrier_lane": self.barrier_lane,
        }
