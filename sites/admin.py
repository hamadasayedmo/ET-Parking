import io
import zipfile

from django import forms
from django.contrib import admin, messages
from django.http import HttpResponse
from django.urls import path, reverse
from django.utils.html import format_html

from accounts.admin import scope_admin_queryset
from .models import Device, Site, Zone
from .services.iot_provision import provision_site_certs


class DeviceAdminForm(forms.ModelForm):
    class Meta:
        model = Device
        fields = "__all__"
        widgets = {
            "password": forms.PasswordInput(render_value=True),
        }


class ZoneInline(admin.TabularInline):
    model = Zone
    extra = 0
    fields = (
        "name",
        "parent",
        "is_active",
        "hourly_rate",
        "additional_fee",
    )
    show_change_link = True


@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "project",
        "is_active",
        "hourly_rate",
        "additional_fee",
        "grace_minutes",
        "iot_thing_name",
        "provisioned_at",
    )
    list_filter = ("is_active", "project")
    search_fields = ("name", "project__name")
    list_editable = ("is_active",)
    inlines = [ZoneInline]
    readonly_fields = (
        "iot_thing_name",
        "iot_certificate_id",
        "provisioned_at",
        "generate_certs_button",
    )
    fields = (
        "project",
        "name",
        "is_active",
        "hourly_rate",
        "additional_fee",
        "grace_minutes",
        "iot_thing_name",
        "iot_certificate_id",
        "provisioned_at",
        "generate_certs_button",
    )

    def get_queryset(self, request):
        return scope_admin_queryset(super().get_queryset(request), request)

    @admin.display(description="Certificates")
    def generate_certs_button(self, obj: Site):
        if not obj.pk:
            return "Save the site first, then generate certs."
        url = reverse("admin:sites_site_generate_certs", args=[obj.pk])
        label = "Regenerate certs" if obj.iot_certificate_id else "Generate certs"
        return format_html(
            '<a class="button" href="{}">{}</a>'
            '<p class="help">Downloads a zip with device cert, private key, '
            "Amazon Root CA, and mqtt.env.example. "
            "The private key is shown only in this download.</p>",
            url,
            label,
        )

    def get_urls(self):
        custom = [
            path(
                "<path:object_id>/generate-certs/",
                self.admin_site.admin_view(self.generate_certs_view),
                name="sites_site_generate_certs",
            ),
        ]
        return custom + super().get_urls()

    def generate_certs_view(self, request, object_id):
        site = self.get_object(request, object_id)
        if site is None:
            messages.error(request, "Site not found.")
            return self._redirect_changelist()

        try:
            result = provision_site_certs(site)
        except Exception as exc:
            messages.error(request, f"Failed to generate certs: {exc}")
            return self._redirect_change(site.pk)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("device.pem.crt", result.certificate_pem)
            zf.writestr("private.pem.key", result.private_key)
            zf.writestr("AmazonRootCA1.pem", result.ca_pem)
            zf.writestr(
                "mqtt.env.example",
                (
                    f"# Copy values into the gateway .env / compose env\n"
                    f"SITE_ID={site.pk}\n"
                    f"MQTT_CLIENT_ID={result.mqtt_client_id}\n"
                    f"MQTT_HOST={result.mqtt_host}\n"
                    f"MQTT_PORT={result.mqtt_port}\n"
                    f"MQTT_CA_CERT=certs/AmazonRootCA1.pem\n"
                    f"MQTT_CERT_FILE=certs/device.pem.crt\n"
                    f"MQTT_KEY_FILE=certs/private.pem.key\n"
                    f"GATEWAY_API_TOKEN=change-me\n"
                ),
            )

        filename = f"site-{site.pk}-certs.zip"
        response = HttpResponse(buf.getvalue(), content_type="application/zip")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    def _redirect_changelist(self):
        from django.shortcuts import redirect

        return redirect("admin:sites_site_changelist")

    def _redirect_change(self, object_id):
        from django.shortcuts import redirect

        return redirect("admin:sites_site_change", object_id)


@admin.register(Zone)
class ZoneAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "site",
        "parent",
        "is_active",
        "hourly_rate",
        "additional_fee",
    )
    list_filter = ("is_active", "site", "site__project")
    search_fields = ("name", "site__name")
    list_editable = ("is_active",)

    def get_queryset(self, request):
        return scope_admin_queryset(
            super().get_queryset(request), request, field="site__project_id"
        )


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    form = DeviceAdminForm
    list_display = (
        "id",
        "name",
        "site",
        "zone",
        "ip",
        "type",
        "barrier_lane",
        "enabled",
        "gate_locked",
        "pulse_button",
        "created_at",
    )
    list_filter = ("site", "zone", "type", "enabled", "gate_locked")
    search_fields = ("name", "ip", "username")
    readonly_fields = ("site", "pulse_button", "created_at", "updated_at")
    fields = (
        "zone",
        "site",
        "name",
        "ip",
        "username",
        "password",
        "type",
        "barrier_lane",
        "enabled",
        "gate_locked",
        "pulse_button",
        "created_at",
        "updated_at",
    )

    def get_queryset(self, request):
        return scope_admin_queryset(
            super().get_queryset(request), request, field="site__project_id"
        )

    @admin.display(description="Open gate")
    def pulse_button(self, obj: Device):
        if not obj.pk:
            return "Save the device first, then pulse."
        url = reverse("admin:sites_device_pulse", args=[obj.pk])
        return format_html('<a class="button" href="{}">Pulse</a>', url)

    def get_urls(self):
        custom = [
            path(
                "<path:object_id>/pulse/",
                self.admin_site.admin_view(self.pulse_view),
                name="sites_device_pulse",
            ),
        ]
        return custom + super().get_urls()

    def pulse_view(self, request, object_id):
        from django.shortcuts import redirect

        from .views import send_device_command

        device = self.get_object(request, object_id)
        if device is None:
            messages.error(request, "Device not found.")
            return redirect("admin:sites_device_changelist")

        if not device.enabled:
            messages.error(request, "Device is disabled; enable it before pulsing.")
            return redirect("admin:sites_device_change", device.pk)

        ok, message = send_device_command(device, "pulse")
        if ok:
            messages.success(request, message)
        else:
            messages.error(request, message)
        return redirect("admin:sites_device_change", device.pk)
