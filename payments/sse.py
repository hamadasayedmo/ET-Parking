"""SSE stream of open PaymentIntents for one exit kiosk device."""

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

from accounts.scoping import apply_project_scope, user_project_ids
from payments.intent_bus import payment_intent_bus
from payments.models import KioskCredential, PaymentIntent
from payments.serializers import PaymentIntentSerializer
from sites.models import Device

User = get_user_model()
logger = logging.getLogger(__name__)

OPEN = (PaymentIntent.Status.CREATED, PaymentIntent.Status.PENDING)
HEARTBEAT_SECONDS = 5
BUS_WAIT_SECONDS = 0.75
RECONCILE_SECONDS = 1.0
_SSE_PAD = ": " + ("*" * 2048) + "\n\n"


def _sse_format(event: str, data: dict) -> bytes:
    payload = json.dumps(data, default=str)
    return f"event: {event}\ndata: {payload}\n\n{_SSE_PAD}".encode("utf-8")


def _sse_comment(text: str = "keepalive") -> bytes:
    return f": {text}\n\n{_SSE_PAD}".encode("utf-8")


def _as_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_raw_token(request) -> str:
    raw = (request.GET.get("access_token") or request.GET.get("token") or "").strip()
    auth = request.headers.get("Authorization", "")
    if not raw and auth.lower().startswith("bearer "):
        raw = auth[7:].strip()
    if not raw:
        raw = (request.headers.get("X-Kiosk-Token") or "").strip()
    return raw


def _authenticate_sse(request):
    """
    Returns (user_or_None, credential_or_None, error_response_or_None).
    Accepts kiosk API token or staff JWT.
    """
    from accounts.permissions import user_in_ops_role

    raw = _extract_raw_token(request)
    if raw:
        credential = (
            KioskCredential.objects.select_related(
                "device", "device__site", "device__site__project"
            )
            .filter(token=raw, is_active=True)
            .first()
        )
        if credential is not None:
            return None, credential, None

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


def _serialize_intent(intent: PaymentIntent) -> dict:
    data = dict(PaymentIntentSerializer(intent).data)
    device = intent.access_request.device if intent.access_request_id else None
    data["device_id"] = device.pk if device else None
    data["project_id"] = intent.project_id
    return data


def _open_map(*, device_id: int, user=None) -> dict[int, dict]:
    close_old_connections()
    qs = (
        PaymentIntent.objects.select_related(
            "session",
            "session__site",
            "project",
            "access_request",
            "access_request__device",
        )
        .filter(
            access_request__device_id=device_id,
            status__in=OPEN,
        )
        .order_by("created_at")
    )
    if user is not None and getattr(user, "is_authenticated", False):
        qs = apply_project_scope(qs, user, field="project_id")
    out = {}
    for intent in qs:
        out[int(intent.pk)] = _serialize_intent(intent)
    return out


def _payload_matches(payload: dict, *, device_id: int, allowed_project_ids) -> bool:
    value = payload.get("device_id")
    if value is None or int(value) != int(device_id):
        return False
    if allowed_project_ids is not None:
        project_id = payload.get("project_id")
        if project_id is None or int(project_id) not in allowed_project_ids:
            return False
    return True


def _queue_get(q: queue.Queue, timeout: float):
    try:
        return q.get(timeout=timeout)
    except queue.Empty:
        return None


async def _astream(*, device_id: int, user=None) -> AsyncIterator[bytes]:
    allowed = None
    if user is not None and getattr(user, "is_authenticated", False):
        allowed = await sync_to_async(user_project_ids, thread_sensitive=True)(user)

    q = await sync_to_async(payment_intent_bus.subscribe, thread_sensitive=True)()
    try:
        known = await sync_to_async(_open_map, thread_sensitive=True)(
            device_id=device_id, user=user
        )
        yield _sse_format("snapshot", {"pending": list(known.values())})
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
                    device_id=device_id,
                    allowed_project_ids=allowed,
                ):
                    iid = int(payload["id"])
                    if event == "payment_intent.pending":
                        known[iid] = payload
                        yield _sse_format(event, payload)
                        await asyncio.sleep(0)
                    elif event == "payment_intent.removed":
                        known.pop(iid, None)
                        yield _sse_format(event, payload)
                        await asyncio.sleep(0)

            now = time.monotonic()
            if now - last_reconcile >= RECONCILE_SECONDS:
                try:
                    current = await sync_to_async(_open_map, thread_sensitive=True)(
                        device_id=device_id, user=user
                    )
                    for iid, row in current.items():
                        if iid not in known:
                            known[iid] = row
                            yield _sse_format("payment_intent.pending", row)
                            await asyncio.sleep(0)
                    for iid, previous in list(known.items()):
                        if iid not in current:
                            known.pop(iid, None)
                            yield _sse_format(
                                "payment_intent.removed",
                                {
                                    "id": iid,
                                    "status": previous.get("status", "unknown"),
                                    "device_id": previous.get("device_id"),
                                    "project_id": previous.get("project_id"),
                                },
                            )
                            await asyncio.sleep(0)
                    known = current
                except Exception:
                    logger.exception("kiosk SSE reconcile failed")
                    await sync_to_async(close_old_connections, thread_sensitive=True)()
                last_reconcile = now

            if now - last_heartbeat >= HEARTBEAT_SECONDS:
                yield _sse_format("ping", {"ts": int(now)})
                await asyncio.sleep(0)
                yield _sse_comment("keepalive")
                await asyncio.sleep(0)
                last_heartbeat = now
    finally:
        await sync_to_async(payment_intent_bus.unsubscribe, thread_sensitive=True)(q)
        await sync_to_async(close_old_connections, thread_sensitive=True)()


def build_kiosk_payment_sse_response(request):
    user, credential, error = _authenticate_sse(request)
    if error is not None:
        return error

    if credential is not None:
        device = credential.device
        if device.type != Device.Type.EXIT:
            return HttpResponse("Kiosk is not linked to an exit device.", status=400)
        device_id = device.pk
        param = _as_int(request.GET.get("device"))
        if param is not None and param != device_id:
            return HttpResponse("device does not match kiosk credential.", status=403)
        stream_user = None
    else:
        device_id = _as_int(request.GET.get("device"))
        if device_id is None:
            return HttpResponse("device query param is required.", status=400)
        device = (
            Device.objects.select_related("site")
            .filter(pk=device_id, type=Device.Type.EXIT)
            .first()
        )
        if device is None:
            return HttpResponse("Exit device not found.", status=404)
        allowed = user_project_ids(user)
        if allowed is not None and device.site.project_id not in allowed:
            return HttpResponse("Forbidden project.", status=403)
        stream_user = user

    response = StreamingHttpResponse(
        _astream(device_id=device_id, user=stream_user),
        content_type="text/event-stream; charset=utf-8",
    )
    response["Cache-Control"] = "no-cache, no-store, no-transform"
    response["X-Accel-Buffering"] = "no"
    response["Connection"] = "keep-alive"
    response["Content-Encoding"] = "identity"
    response.streaming = True
    return response


@csrf_exempt
def kiosk_payment_sse(request):
    if request.method != "GET":
        return HttpResponse(status=405)
    return build_kiosk_payment_sse_response(request)
