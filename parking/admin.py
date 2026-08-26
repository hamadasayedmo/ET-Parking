from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from accounts.admin import scope_admin_queryset
from .models import AccessRequest, Event, Session, SessionAuditEntry
from .services import approve_access_request, deny_access_request


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "device",
        "plate",
        "vehicle_type",
        "action",
        "decision",
        "created_at",
    )
    list_filter = ("decision", "action", "vehicle_type", "device__site")
    search_fields = ("plate", "vehicle_type")
    readonly_fields = ("created_at",)

    def get_queryset(self, request):
        return scope_admin_queryset(
            super().get_queryset(request),
            request,
            field="device__site__project_id",
        )


@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "plate",
        "wallet",
        "site",
        "start_time",
        "end_time",
        "fee",
        "payment_status",
        "billing_method",
        "paid_exit_until",
    )
    list_filter = ("payment_status", "billing_method", "site")
    search_fields = ("plate", "wallet__name")
    autocomplete_fields = ("wallet",)
    readonly_fields = (
        "plate",
        "wallet",
        "site",
        "entry_device",
        "start_event",
        "start_time",
        "end_event",
        "end_time",
        "fee",
        "payment_status",
        "billing_method",
        "paid_exit_until",
        "billing_exempt",
        "allow_negative_balance",
    )

    def get_queryset(self, request):
        return scope_admin_queryset(
            super().get_queryset(request), request, field="site__project_id"
        )


@admin.register(SessionAuditEntry)
class SessionAuditEntryAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "session",
        "action",
        "field",
        "actor",
        "created_at",
    )
    list_filter = ("action",)
    search_fields = ("session__plate", "note", "old_value", "new_value")
    readonly_fields = (
        "session",
        "actor",
        "action",
        "field",
        "old_value",
        "new_value",
        "note",
        "created_at",
    )

    def get_queryset(self, request):
        return scope_admin_queryset(
            super().get_queryset(request),
            request,
            field="session__site__project_id",
        )


@admin.register(AccessRequest)
class AccessRequestAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "plate",
        "vehicle_type",
        "device",
        "site_name",
        "action",
        "status",
        "created_at",
        "queue_actions",
    )
    list_filter = ("status", "action", "vehicle_type", "device__site")
    search_fields = ("plate", "vehicle_type", "reason", "decision_note")
    readonly_fields = (
        "device",
        "plate",
        "vehicle_type",
        "action",
        "status",
        "reason",
        "decision_note",
        "exempted",
        "decided_by",
        "decided_at",
        "resolution_event",
        "created_at",
    )
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "device",
                    "plate",
                    "vehicle_type",
                    "action",
                    "status",
                    "created_at",
                )
            },
        ),
        (
            "Escalation",
            {
                "description": (
                    "System reason is set by the platform when the request is "
                    "created or updated. Operator note is entered on approve/deny."
                ),
                "fields": ("reason", "decision_note"),
            },
        ),
        (
            "Decision",
            {
                "fields": (
                    "exempted",
                    "decided_by",
                    "decided_at",
                    "resolution_event",
                )
            },
        ),
    )
    ordering = ("-created_at",)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        qs = qs.select_related("device", "device__site", "decided_by")
        return scope_admin_queryset(
            qs, request, field="device__site__project_id"
        )

    @admin.display(description="Site", ordering="device__site__name")
    def site_name(self, obj):
        return obj.device.site.name

    @admin.display(description="Actions")
    def queue_actions(self, obj):
        if obj.status != AccessRequest.Status.PENDING:
            return "—"
        approve_url = reverse(
            "admin:parking_accessrequest_approve", args=[obj.pk]
        )
        deny_url = reverse("admin:parking_accessrequest_deny", args=[obj.pk])
        return format_html(
            '<a class="button" href="{}">Approve</a>&nbsp;'
            '<a class="button" href="{}">Deny</a>',
            approve_url,
            deny_url,
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.user.has_perm(
            "parking.view_accessrequest"
        ) or request.user.has_perm("parking.decide_accessrequest")

    def has_delete_permission(self, request, obj=None):
        return False

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<int:request_id>/approve/",
                self.admin_site.admin_view(self.approve_view),
                name="parking_accessrequest_approve",
            ),
            path(
                "<int:request_id>/deny/",
                self.admin_site.admin_view(self.deny_view),
                name="parking_accessrequest_deny",
            ),
        ]
        return custom + urls

    def _require_decide(self, request):
        if not request.user.has_perm("parking.decide_accessrequest"):
            raise PermissionDenied

    def _decide_view(self, request, request_id, *, action: str):
        self._require_decide(request)
        access_request = AccessRequest.objects.select_related(
            "device", "device__site", "device__site__project"
        ).get(pk=request_id)

        if access_request.status != AccessRequest.Status.PENDING:
            self.message_user(
                request,
                f"Request {access_request.pk} is not pending.",
                messages.ERROR,
            )
            return redirect("admin:parking_accessrequest_changelist")

        needs_entry_time = False
        if action == "approve":
            from parking.services import access_request_requires_entry_time

            needs_entry_time = access_request_requires_entry_time(access_request)
        decision_note = ""
        exempted = False
        entry_time = ""

        if request.method == "POST":
            decision_note = request.POST.get("decision_note", "")
            exempted = request.POST.get("exempted") == "on"
            entry_time = (request.POST.get("entry_time") or "").strip()
            try:
                if action == "approve":
                    approve_access_request(
                        access_request,
                        request.user,
                        note=decision_note,
                        exempted=exempted,
                        entry_time=entry_time or None,
                    )
                    if exempted:
                        msg = (
                            f"Approved {access_request.plate} (exempted) — "
                            "barrier pulse sent, no wallet charge."
                        )
                    elif needs_entry_time:
                        msg = (
                            f"Approved {access_request.plate} — session started "
                            "from entry time, fee charged, barrier pulse sent."
                        )
                    else:
                        msg = (
                            f"Approved {access_request.plate} — "
                            "barrier pulse sent."
                        )
                    self.message_user(request, msg, messages.SUCCESS)
                else:
                    deny_access_request(
                        access_request, request.user, note=decision_note
                    )
                    self.message_user(
                        request,
                        f"Denied {access_request.plate} — barrier stays closed.",
                        messages.SUCCESS,
                    )
            except ValueError as exc:
                self.message_user(request, str(exc), messages.ERROR)
                if action == "approve" and needs_entry_time:
                    context = {
                        **self.admin_site.each_context(request),
                        "opts": self.model._meta,
                        "access_request": access_request,
                        "action": action,
                        "title": f"{action.title()} access request",
                        "needs_entry_time": needs_entry_time,
                        "decision_note": decision_note,
                        "exempted": exempted,
                        "entry_time": entry_time,
                    }
                    return TemplateResponse(
                        request,
                        "admin/parking/accessrequest/decide.html",
                        context,
                    )
            except Exception as exc:
                if action == "approve":
                    self.message_user(
                        request,
                        f"Approved in DB but pulse may have failed: {exc}",
                        messages.WARNING,
                    )
                else:
                    self.message_user(request, str(exc), messages.ERROR)
            else:
                return redirect("admin:parking_accessrequest_changelist")
            return redirect("admin:parking_accessrequest_changelist")

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "access_request": access_request,
            "action": action,
            "title": f"{action.title()} access request",
            "needs_entry_time": needs_entry_time and action == "approve",
            "decision_note": decision_note,
            "exempted": exempted,
            "entry_time": entry_time,
        }
        return TemplateResponse(
            request,
            "admin/parking/accessrequest/decide.html",
            context,
        )

    def approve_view(self, request, request_id):
        return self._decide_view(request, request_id, action="approve")

    def deny_view(self, request, request_id):
        return self._decide_view(request, request_id, action="deny")

    def changelist_view(self, request, extra_context=None):
        if "status__exact" not in request.GET and "status" not in request.GET:
            q = request.GET.copy()
            q["status__exact"] = AccessRequest.Status.PENDING
            request.GET = q
            request.META["QUERY_STRING"] = q.urlencode()
        return super().changelist_view(request, extra_context=extra_context)
