"""Project membership helpers for staff users."""

from __future__ import annotations

from django.db.models import QuerySet


def user_project_ids(user) -> set[int] | None:
    """
    Return the set of project PKs the user may access, or None if unrestricted.

    Rules:
      - anonymous → empty set
      - superuser → unrestricted (None)
      - no profile / all_projects=True → unrestricted (None)
      - all_projects=False → assigned project IDs (may be empty)
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return set()
    if getattr(user, "is_superuser", False):
        return None

    profile = getattr(user, "staff_profile", None)
    if profile is None or profile.all_projects:
        return None

    return set(profile.projects.values_list("id", flat=True))


def user_can_access_project(user, project_id: int | str | None) -> bool:
    if project_id is None:
        return False
    allowed = user_project_ids(user)
    if allowed is None:
        return True
    try:
        return int(project_id) in allowed
    except (TypeError, ValueError):
        return False


def apply_project_scope(
    qs: QuerySet,
    user,
    *,
    field: str = "project_id",
) -> QuerySet:
    """Filter a queryset to the user's allowed projects."""
    allowed = user_project_ids(user)
    if allowed is None:
        return qs
    if not allowed:
        return qs.none()
    return qs.filter(**{f"{field}__in": allowed})


def scoped_project_queryset(user) -> QuerySet:
    from projects.models import Project

    return apply_project_scope(Project.objects.all(), user, field="id")
