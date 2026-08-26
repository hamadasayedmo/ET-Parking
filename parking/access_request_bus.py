"""
Pub/sub for AccessRequest SSE fans.

- Always fans out in-process (same worker).
- When REDIS_URL is set, also PUBLISH so mqtt_worker → web crosses process
  boundaries without client polling.
- Without REDIS_URL: in-process only (fine for local tests).
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

REDIS_CHANNEL = "parking:access_request"


def _redis_url() -> str:
    return (getattr(settings, "REDIS_URL", None) or "").strip()


class AccessRequestEventBus:
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
        """Deliver to local SSE subscribers and cross-process via Redis."""
        self._fanout_local(event, data)
        self._publish_redis(event, data)

    def _fanout_local(self, event: str, data: dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        if subscribers:
            logger.info(
                "SSE fanout %s id=%s subscribers=%s",
                event,
                data.get("id"),
                len(subscribers),
            )
        for q in subscribers:
            try:
                q.put_nowait((event, data))
            except queue.Full:
                # Drop if a slow client falls behind.
                pass

    def _publish_redis(self, event: str, data: dict[str, Any]) -> None:
        url = _redis_url()
        if not url:
            logger.warning(
                "REDIS_URL unset — access_request %s stays in-process only",
                event,
            )
            return
        envelope = {
            "event": event,
            "data": data,
            "pid": os.getpid(),
        }
        payload = json.dumps(envelope, default=str)
        try:
            import redis

            client = redis.Redis.from_url(url, decode_responses=True)
            try:
                receivers = client.publish(REDIS_CHANNEL, payload)
            finally:
                client.close()
            logger.info(
                "Redis PUBLISH %s id=%s receivers=%s",
                event,
                data.get("id"),
                receivers,
            )
        except Exception:
            logger.exception("Redis PUBLISH failed for %s", event)

    def ensure_redis_listener(self) -> None:
        """Start the Redis SUBSCRIBE thread once per process."""
        if not _redis_url():
            return
        with self._listener_lock:
            if self._listener_started:
                return
            thread = threading.Thread(
                target=self._redis_listen_loop,
                name="access-request-redis-listen",
                daemon=True,
            )
            thread.start()
            self._listener_started = True
            logger.info(
                "Started Redis SUBSCRIBE thread for %s", REDIS_CHANNEL
            )

    def _redis_listen_loop(self) -> None:
        """Dedicated connection: SUBSCRIBE and fan remote messages into local queues."""
        import redis

        url = _redis_url()
        while True:
            pubsub = None
            client = None
            try:
                client = redis.Redis.from_url(url, decode_responses=True)
                pubsub = client.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(REDIS_CHANNEL)
                logger.info("Listening on Redis channel %s", REDIS_CHANNEL)
                for message in pubsub.listen():
                    if message is None or message.get("type") != "message":
                        continue
                    logger.debug(
                        "Redis message on %s (%s bytes)",
                        REDIS_CHANNEL,
                        len(str(message.get("data") or "")),
                    )
                    self._handle_remote_payload(message.get("data"))
            except Exception:
                logger.exception(
                    "Redis SUBSCRIBE loop error; reconnecting in 2s"
                )
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
            logger.warning("Invalid Redis payload: %s", str(raw)[:200])
            return

        # Same process already got a local fanout on publish.
        if envelope.get("pid") == os.getpid():
            return

        event = envelope.get("event")
        data = envelope.get("data") or {}
        if not event or not isinstance(data, dict):
            return

        self._fanout_local(event, data)


access_request_bus = AccessRequestEventBus()
