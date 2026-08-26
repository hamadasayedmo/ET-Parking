from decimal import Decimal
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework_simplejwt.tokens import RefreshToken

from parking.access_request_bus import access_request_bus
from parking.models import AccessRequest
from parking.services import authorize_plate, deny_access_request
from projects.models import Project
from sites.models import Device, Site


User = get_user_model()


async def _read_until_snapshot(streaming_content) -> str:
    chunks: list[str] = []
    async for chunk in streaming_content:
        chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk))
        body = "".join(chunks)
        if "event: snapshot" in body:
            return body
    return "".join(chunks)


class AccessRequestSSETests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="SSE Lot")
        self.site = Site.objects.create(
            project=self.project,
            name="Lot",
            hourly_rate=Decimal("2.00"),
            additional_fee=Decimal("1.00"),
        )
        with patch("sites.signals.publish_once", return_value=True):
            self.entry = Device.objects.create(
                site=self.site,
                ip="10.0.0.1",
                username="admin",
                password="secret",
                type=Device.Type.ENTRY,
            )
        self.staff = User.objects.create_user(
            username="ops",
            password="pass",
            is_staff=True,
        )
        self.token = str(RefreshToken.for_user(self.staff).access_token)

    def test_signal_publishes_pending_and_removed(self):
        q = access_request_bus.subscribe()
        try:
            with patch("parking.services.publish_once", return_value=True):
                result = authorize_plate(self.entry, "SSE001")
            event, payload = q.get(timeout=2)
            self.assertEqual(event, "access_request.pending")
            self.assertEqual(payload["plate"], "SSE001")
            self.assertEqual(payload["status"], AccessRequest.Status.PENDING)

            deny_access_request(result.access_request, self.staff)
            event, payload = q.get(timeout=2)
            self.assertEqual(event, "access_request.removed")
            self.assertEqual(payload["status"], AccessRequest.Status.DENIED)
        finally:
            access_request_bus.unsubscribe(q)

    def test_sse_snapshot_requires_auth(self):
        res = self.client.get("/api/access-requests/stream/")
        self.assertEqual(res.status_code, 401)

    def test_sse_snapshot_with_token(self):
        with patch("parking.services.publish_once", return_value=True):
            authorize_plate(self.entry, "SSE002")

        res = self.client.get(
            f"/api/access-requests/stream/?access_token={self.token}",
            HTTP_ACCEPT="text/event-stream",
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/event-stream", res["Content-Type"])

        body = async_to_sync(_read_until_snapshot)(res.streaming_content)
        self.assertIn("event: snapshot", body)
        self.assertIn("SSE002", body)
        res.close()

    def test_stream_action_not_treated_as_pk(self):
        """Without a list action, DRF would treat stream as pk and return 406."""
        res = self.client.get(
            f"/api/access-requests/stream/?access_token={self.token}",
            HTTP_ACCEPT="text/event-stream",
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/event-stream", res.get("Content-Type", ""))
        res.close()
