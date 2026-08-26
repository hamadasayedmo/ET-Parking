from rest_framework import serializers

from sites.models import Device, Site, Zone


class SiteSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source="project.name", read_only=True)
    device_count = serializers.SerializerMethodField()
    entry_device_count = serializers.SerializerMethodField()
    exit_device_count = serializers.SerializerMethodField()
    zone_count = serializers.SerializerMethodField()

    class Meta:
        model = Site
        fields = (
            "id",
            "project",
            "project_name",
            "name",
            "is_active",
            "hourly_rate",
            "additional_fee",
            "grace_minutes",
            "iot_thing_name",
            "iot_certificate_id",
            "provisioned_at",
            "device_count",
            "entry_device_count",
            "exit_device_count",
            "zone_count",
        )
        read_only_fields = (
            "is_active",
            "iot_thing_name",
            "iot_certificate_id",
            "provisioned_at",
            "project_name",
            "device_count",
            "entry_device_count",
            "exit_device_count",
            "zone_count",
        )

    def get_device_count(self, obj):
        value = getattr(obj, "annotated_device_count", None)
        if value is not None:
            return value
        return obj.devices.count()

    def get_entry_device_count(self, obj):
        value = getattr(obj, "entry_device_count", None)
        if value is not None and not callable(value):
            return value
        return obj.devices.filter(type=Device.Type.ENTRY).count()

    def get_exit_device_count(self, obj):
        value = getattr(obj, "exit_device_count", None)
        if value is not None and not callable(value):
            return value
        return obj.devices.filter(type=Device.Type.EXIT).count()

    def get_zone_count(self, obj):
        value = getattr(obj, "annotated_zone_count", None)
        if value is not None:
            return value
        return obj.zones.count()


class ZoneSerializer(serializers.ModelSerializer):
    site_name = serializers.CharField(source="site.name", read_only=True)
    parent_name = serializers.CharField(source="parent.name", read_only=True)
    project = serializers.IntegerField(source="site.project_id", read_only=True)
    device_count = serializers.SerializerMethodField()

    class Meta:
        model = Zone
        fields = (
            "id",
            "site",
            "site_name",
            "project",
            "parent",
            "parent_name",
            "name",
            "is_active",
            "hourly_rate",
            "additional_fee",
            "device_count",
        )
        read_only_fields = (
            "site_name",
            "parent_name",
            "project",
            "device_count",
        )

    def get_device_count(self, obj):
        value = getattr(obj, "annotated_device_count", None)
        if value is not None:
            return value
        return obj.devices.count()

    def validate(self, attrs):
        site = attrs.get("site") or getattr(self.instance, "site", None)
        parent = attrs.get("parent", serializers.empty)
        if parent is serializers.empty:
            parent = getattr(self.instance, "parent", None)
        if parent is not None and site is not None and parent.site_id != site.pk:
            raise serializers.ValidationError(
                {"parent": "Parent zone must belong to the same site."}
            )
        return attrs


class DeviceSerializer(serializers.ModelSerializer):
    site_name = serializers.CharField(source="site.name", read_only=True)
    zone_name = serializers.CharField(source="zone.name", read_only=True)
    zone_parent = serializers.IntegerField(
        source="zone.parent_id", read_only=True, allow_null=True
    )
    zone_parent_name = serializers.CharField(
        source="zone.parent.name", read_only=True, allow_null=True
    )
    project = serializers.IntegerField(source="site.project_id", read_only=True)
    project_name = serializers.CharField(source="site.project.name", read_only=True)
    password = serializers.CharField(write_only=True, required=False)
    has_kiosk = serializers.SerializerMethodField()
    kiosk_last_used_at = serializers.SerializerMethodField()

    class Meta:
        model = Device
        fields = (
            "id",
            "site",
            "site_name",
            "zone",
            "zone_name",
            "zone_parent",
            "zone_parent_name",
            "project",
            "project_name",
            "name",
            "ip",
            "username",
            "password",
            "type",
            "barrier_lane",
            "enabled",
            "gate_locked",
            "has_kiosk",
            "kiosk_last_used_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "site",
            "created_at",
            "updated_at",
            "site_name",
            "zone_name",
            "zone_parent",
            "zone_parent_name",
            "project",
            "project_name",
            "gate_locked",
            "has_kiosk",
            "kiosk_last_used_at",
        )

    def _kiosk_credential(self, obj):
        # Reverse OneToOne; select_related in the viewset avoids N+1.
        try:
            return obj.kiosk_credential
        except Exception:
            return None

    def get_has_kiosk(self, obj):
        if obj.type != Device.Type.EXIT:
            return None
        cred = self._kiosk_credential(obj)
        return bool(cred and cred.is_active)

    def get_kiosk_last_used_at(self, obj):
        cred = self._kiosk_credential(obj)
        return cred.last_used_at if cred else None

    def validate(self, attrs):
        if self.instance is None and not attrs.get("password"):
            raise serializers.ValidationError(
                {"password": "This field is required."}
            )
        zone = attrs.get("zone") or getattr(self.instance, "zone", None)
        if zone is None and self.instance is None:
            raise serializers.ValidationError({"zone": "This field is required."})
        return attrs

    def create(self, validated_data):
        zone = validated_data["zone"]
        validated_data["site"] = zone.site
        return super().create(validated_data)

    def update(self, instance, validated_data):
        zone = validated_data.get("zone", instance.zone)
        validated_data["site"] = zone.site
        return super().update(instance, validated_data)
