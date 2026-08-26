"""
Pub/sub for PaymentIntent SSE (exit kiosk).

Same pattern as parking.access_request_bus:
- in-process fanout
- Redis PUBLISH when REDIS_URL is set (cross-worker)
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

REDIS_CHANNEL = "parking:payment_intent"


def _redis_url() -> str:
    return (getattr(settings, "REDIS_URL", None) or "").strip()


class PaymentIntentEventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        self._listener_lock = threading.Lock()
        self._listener_started = False

    def subscribe(self) -> queue.Queue:
        self.ensure_redis_listener()
        q: queue.Queue = queue.Queue(maxsize=256)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, event: str, data: dict[str, Any]) -> None:
        self._fanout_local(event, data)
        self._publish_redis(event, data)

    def _fanout_local(self, event: str, data: dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait((event, data))
            except queue.Full:
                pass

    def _publish_redis(self, event: str, data: dict[str, Any]) -> None:
        url = _redis_url()
        if not url:
            return
        envelope = {"event": event, "data": data, "pid": os.getpid()}
        try:
            import redis

            client = redis.Redis.from_url(url, decode_responses=True)
            try:
                client.publish(REDIS_CHANNEL, json.dumps(envelope, default=str))
            finally:
                client.close()
        except Exception:
            logger.exception("Redis PUBLISH failed for payment_intent %s", event)

    def ensure_redis_listener(self) -> None:
        if not _redis_url():
            return
        with self._listener_lock:
            if self._listener_started:
                return
            thread = threading.Thread(
                target=self._redis_listen_loop,
                name="payment-intent-redis-listen",
                daemon=True,
            )
            thread.start()
            self._listener_started = True

    def _redis_listen_loop(self) -> None:
        import redis

        url = _redis_url()
        while True:
            pubsub = None
            client = None
            try:
                client = redis.Redis.from_url(url, decode_responses=True)
                pubsub = client.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(REDIS_CHANNEL)
                for message in pubsub.listen():
                    if message is None or message.get("type") != "message":
                        continue
                    self._handle_remote_payload(message.get("data"))
            except Exception:
                logger.exception("payment_intent Redis SUBSCRIBE error; retry in 2s")
                time.sleep(2)
            finally:
                if pubsub is not None:
                    try:
                        pubsub.close()
                    except Exception:
                        pass
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass

    def _handle_remote_payload(self, raw: str | None) -> None:
        if not raw:
            return
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            return
        if envelope.get("pid") == os.getpid():
            return
        event = envelope.get("event")
        data = envelope.get("data") or {}
        if event and isinstance(data, dict):
            self._fanout_local(event, data)


payment_intent_bus = PaymentIntentEventBus()
