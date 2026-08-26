from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.forms.models import BaseInlineFormSet

from accounts.scoping import apply_project_scope

from .models import Cashier, StaffProfile

User = get_user_model()


class StaffProfileFormSet(BaseInlineFormSet):
    """
    UserAdmin add creates the User first; ensure_staff_profile then inserts a
    StaffProfile. The inline would INSERT again and hit the OneToOne unique
    constraint — update the signal-created row instead.
    """

    def save_new(self, form, commit=True):
        obj = form.save(commit=False)
        existing = StaffProfile.objects.filter(user_id=obj.user_id).first()
        if existing is not None:
            obj.pk = existing.pk
            obj.id = existing.pk
            obj._state.adding = False
            obj._state.db = existing._state.db
        if commit:
            obj.save()
            form.save_m2m()
        return obj


class StaffProfileInline(admin.StackedInline):
    model = StaffProfile
    formset = StaffProfileFormSet
    can_delete = False
    extra = 0
    max_num = 1
    filter_horizontal = ("projects",)
    verbose_name_plural = "Project access"
    fk_name = "user"


class CashierInline(admin.TabularInline):
    model = Cashier
    extra = 0
    autocomplete_fields = ("zone",)
    verbose_name_plural = "Cashier zones"


class UserAdmin(DjangoUserAdmin):
    inlines = (StaffProfileInline, CashierInline)
    list_display = DjangoUserAdmin.list_display + ("project_scope_summary",)

    def get_inline_instances(self, request, obj=None):
        # On add, show one empty project-access form (extra=0 would hide it).
        inlines = super().get_inline_instances(request, obj)
        if obj is None:
            for inline in inlines:
                if isinstance(inline, StaffProfileInline):
                    inline.extra = 1
        return inlines

    @admin.display(description="Projects")
    def project_scope_summary(self, obj):
        if obj.is_superuser:
            return "All (superuser)"
        profile = getattr(obj, "staff_profile", None)
        if profile is None or profile.all_projects:
            return "All"
        names = list(profile.projects.order_by("name").values_list("name", flat=True))
        if not names:
            return "None (locked)"
        if len(names) <= 3:
            return ", ".join(names)
        return f"{', '.join(names[:3])} (+{len(names) - 3})"


admin.site.unregister(User)
admin.site.register(User, UserAdmin)


@admin.register(StaffProfile)
class StaffProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "all_projects", "project_count")
    list_filter = ("all_projects",)
    search_fields = ("user__username", "user__email")
    filter_horizontal = ("projects",)
    autocomplete_fields = ("user",)

    @admin.display(description="Assigned")
    def project_count(self, obj):
        if obj.all_projects:
            return "—"
        return obj.projects.count()


@admin.register(Cashier)
class CashierAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "zone", "site_name", "created_at")
    list_filter = ("zone__site__project", "zone__site")
    search_fields = (
        "user__username",
        "zone__name",
        "zone__site__name",
    )
    autocomplete_fields = ("user", "zone")

    @admin.display(description="Site", ordering="zone__site__name")
    def site_name(self, obj):
        return obj.zone.site.name


def scope_admin_queryset(qs, request, *, field: str = "project_id"):
    return apply_project_scope(qs, request.user, field=field)
