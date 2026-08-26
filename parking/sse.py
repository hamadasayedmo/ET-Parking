"""SSE stream for pending AccessRequest create / remove events."""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import time
from typing import AsyncIterator

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.http import HttpResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.tokens import AccessToken

from accounts.scoping import user_can_access_project, user_project_ids
from parking.access_request_bus import access_request_bus
from parking.models import AccessRequest
from parking.serializers import AccessRequestSerializer

User = get_user_model()
logger = logging.getLogger(__name__)

PENDING = AccessRequest.Status.PENDING
HEARTBEAT_SECONDS = 5
BUS_WAIT_SECONDS = 0.75
RECONCILE_SECONDS = 1.0
# Defeat reverse-proxy / ALB buffering so EventSource actually receives chunks.
_SSE_PAD = ": " + ("*" * 2048) + "\n\n"


def _sse_format(event: str, data: dict) -> bytes:
    payload = json.dumps(data, default=str)
    text = f"event: {event}\ndata: {payload}\n\n{_SSE_PAD}"
    return text.encode("utf-8")


def _sse_comment(text: str = "keepalive") -> bytes:
    return f": {text}\n\n{_SSE_PAD}".encode("utf-8")


def _as_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _base_queryset():
    return AccessRequest.objects.select_related(
        "device",
        "device__site",
        "device__site__project",
        "device__zone",
        "decided_by",
        "resolution_event",
    )


def _apply_filters(
    qs, *, project_id=None, site_id=None, device_id=None, allowed_project_ids=None
):
    if allowed_project_ids is not None:
        qs = qs.filter(device__site__project_id__in=allowed_project_ids)
    if project_id:
        qs = qs.filter(device__site__project_id=project_id)
    if site_id:
        qs = qs.filter(device__site_id=site_id)
    if device_id:
        qs = qs.filter(device_id=device_id)
    return qs


def _payload_matches(
    payload: dict,
    *,
    project_id=None,
    site_id=None,
    device_id=None,
    allowed_project_ids=None,
) -> bool:
    if allowed_project_ids is not None:
        value = payload.get("project_id")
        if value is None or int(value) not in allowed_project_ids:
            return False
    if device_id is not None:
        value = payload.get("device_id", payload.get("device"))
        if value is None:
            return False
        if int(value) != int(device_id):
            return False
    if site_id is not None:
        value = payload.get("site_id")
        if value is not None and int(value) != int(site_id):
            return False
    if project_id is not None:
        value = payload.get("project_id")
        if value is not None and int(value) != int(project_id):
            return False
    return True


def _authenticate_sse(request):
    """
    Accept:
      - ?access_token=<jwt> (staff / ops role)
      - session-authenticated staff / ops role
      - Authorization: Bearer <jwt>
    """
    from accounts.permissions import user_in_ops_role

    raw = (request.GET.get("access_token") or request.GET.get("token") or "").strip()
    auth = request.headers.get("Authorization", "")
    if not raw and auth.lower().startswith("bearer "):
        raw = auth[7:].strip()

    if raw:
        try:
            token = AccessToken(raw)
            user = User.objects.get(pk=token["user_id"])
        except (InvalidToken, TokenError, User.DoesNotExist, KeyError):
            return None, None, HttpResponse("Invalid access token.", status=401)
        if not user.is_active:
            return None, None, HttpResponse("Forbidden.", status=403)
        if not (user.is_staff or user.is_superuser or user_in_ops_role(user)):
            return None, None, HttpResponse("Forbidden.", status=403)
        return user, None, None

    user = request.user
    if user.is_authenticated and (
        user.is_staff or user.is_superuser or user_in_ops_role(user)
    ):
        return user, None, None

    return None, None, HttpResponse("Authentication required.", status=401)


def _pending_map(
    *, project_id=None, site_id=None, device_id=None, allowed_project_ids=None
) -> dict[int, dict]:
    close_old_connections()
    qs = _apply_filters(
        _base_queryset().filter(status=PENDING),
        project_id=project_id,
        site_id=site_id,
        device_id=device_id,
        allowed_project_ids=allowed_project_ids,
    ).order_by("created_at")
    rows = list(qs)
    data = AccessRequestSerializer(rows, many=True).data
    out = {}
    for item, obj in zip(data, rows):
        item = dict(item)
        item["site_id"] = obj.device.site_id
        item["project_id"] = obj.device.site.project_id
        item["device_id"] = obj.device_id
        out[int(item["id"])] = item
    return out


def _queue_get(q: queue.Queue, timeout: float) -> tuple[str, dict] | None:
    try:
        return q.get(timeout=timeout)
    except queue.Empty:
        return None


async def _astream(
    *,
    project_id=None,
    site_id=None,
    device_id=None,
    allowed_project_ids=None,
) -> AsyncIterator[bytes]:
    """
    Async SSE generator so uvicorn/ASGI flushes chunks promptly.

    Sync StreamingHttpResponse iterators often buffer under ASGI until the
    connection closes — clients see HTTP 200 with zero EventSource messages.
    """
    q = await sync_to_async(access_request_bus.subscribe, thread_sensitive=True)()
    try:
        known = await sync_to_async(_pending_map, thread_sensitive=True)(
            project_id=project_id,
            site_id=site_id,
            device_id=device_id,
            allowed_project_ids=allowed_project_ids,
        )
        logger.info(
            "SSE client connected project=%s pending=%s",
            project_id,
            len(known),
        )
        yield _sse_format(
            "snapshot",
            {"pending_requests": list(known.values())},
        )
        await asyncio.sleep(0)
        yield _sse_comment("connected")
        await asyncio.sleep(0)

        last_heartbeat = time.monotonic()
        last_reconcile = time.monotonic()

        while True:
            item = await sync_to_async(_queue_get, thread_sensitive=True)(
                q, BUS_WAIT_SECONDS
            )
            if item is not None:
                event, payload = item
                if payload and _payload_matches(
                    payload,
                    project_id=project_id,
                    site_id=site_id,
                    device_id=device_id,
                    allowed_project_ids=allowed_project_ids,
                ):
                    rid = int(payload["id"])
                    if event == "access_request.pending":
                        known[rid] = payload
                        logger.info("SSE yield pending id=%s", rid)
                        yield _sse_format(event, payload)
                        await asyncio.sleep(0)
                    elif event == "access_request.removed":
                        known.pop(rid, None)
                        logger.info("SSE yield removed id=%s", rid)
                        yield _sse_format(event, payload)
                        await asyncio.sleep(0)

            now = time.monotonic()
            if now - last_reconcile >= RECONCILE_SECONDS:
                try:
                    current = await sync_to_async(
                        _pending_map, thread_sensitive=True
                    )(
                        project_id=project_id,
                        site_id=site_id,
                        device_id=device_id,
                        allowed_project_ids=allowed_project_ids,
                    )
                    # Diff only — no full snapshot spam (Redis delivers live events).
                    for rid, row in current.items():
                        if rid not in known:
                            known[rid] = row
                            yield _sse_format("access_request.pending", row)
                            await asyncio.sleep(0)
                    for rid, previous in list(known.items()):
                        if rid not in current:

                            def _status_for(pk=rid):
                                close_old_connections()
                                return (
                                    AccessRequest.objects.filter(pk=pk)
                                    .values_list("status", flat=True)
                                    .first()
                                    or "unknown"
                                )

                            status_value = await sync_to_async(
                                _status_for, thread_sensitive=True
                            )()
                            known.pop(rid, None)
                            yield _sse_format(
                                "access_request.removed",
                                {
                                    "id": rid,
                                    "status": status_value,
                                    "device_id": previous.get("device_id")
                                    or previous.get("device"),
                                    "site_id": previous.get("site_id"),
                                    "project_id": previous.get("project_id"),
                                },
                            )
                            await asyncio.sleep(0)
                    known = current
                except Exception:
                    logger.exception("SSE reconcile failed")
                    await sync_to_async(close_old_connections, thread_sensitive=True)()
                last_reconcile = now

            if now - last_heartbeat >= HEARTBEAT_SECONDS:
                yield _sse_format("ping", {"ts": int(now)})
                await asyncio.sleep(0)
                yield _sse_comment("keepalive")
                await asyncio.sleep(0)
                last_heartbeat = now
    finally:
        await sync_to_async(access_request_bus.unsubscribe, thread_sensitive=True)(q)
        await sync_to_async(close_old_connections, thread_sensitive=True)()
        logger.info("SSE client disconnected project=%s", project_id)


def build_access_request_sse_response(request):
    """Build an SSE StreamingHttpResponse (bypasses DRF content negotiation)."""
    user, _terminal, error = _authenticate_sse(request)
    if error is not None:
        return error

    project_id = _as_int(request.GET.get("project"))
    site_id = _as_int(request.GET.get("site"))
    device_id = _as_int(request.GET.get("device"))
    allowed_project_ids = None

    if user is not None:
        allowed_project_ids = user_project_ids(user)
        if project_id is not None and not user_can_access_project(user, project_id):
            return HttpResponse("Forbidden project.", status=403)
        if allowed_project_ids is not None and not allowed_project_ids:
            allowed_project_ids = set()

    response = StreamingHttpResponse(
        _astream(
            project_id=project_id,
            site_id=site_id,
            device_id=device_id,
            allowed_project_ids=allowed_project_ids,
        ),
        content_type="text/event-stream; charset=utf-8",
    )
    response["Cache-Control"] = "no-cache, no-store, no-transform"
    response["X-Accel-Buffering"] = "no"
    response["Connection"] = "keep-alive"
    response["Content-Encoding"] = "identity"
    response.streaming = True
    return response


@csrf_exempt
def access_request_sse(request):
    if request.method != "GET":
        return HttpResponse(status=405)
    return build_access_request_sse_response(request)
