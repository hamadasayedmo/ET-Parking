"""Shared MQTT helpers for the cloud worker and config push."""

from __future__ import annotations

import json
import logging
import ssl
import threading
import time
import uuid

import paho.mqtt.client as mqtt
from django.conf import settings

logger = logging.getLogger(__name__)

# Deduplicate noisy gateway/online storms (edge retries until ack).
_last_gateway_online: dict[int, float] = {}
_GATEWAY_ONLINE_DEDUP_SECONDS = 15.0


def mqtt_configured() -> bool:
    required = (
        settings.MQTT_HOST,
        settings.MQTT_CA_CERT,
        settings.MQTT_CERT_FILE,
        settings.MQTT_KEY_FILE,
    )
    if not all(required):
        return False
    # Treat placeholder host from the sample .env as not configured.
    if "...." in settings.MQTT_HOST:
        return False
    return True


def create_mqtt_client(client_id: str | None = None) -> mqtt.Client:
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id or settings.MQTT_CLIENT_ID,
    )
    client.tls_set(
        ca_certs=settings.MQTT_CA_CERT,
        certfile=settings.MQTT_CERT_FILE,
        keyfile=settings.MQTT_KEY_FILE,
        tls_version=ssl.PROTOCOL_TLS_CLIENT,
    )
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    return client


def _disconnect_is_failure(reason_code) -> bool:
    """True if disconnect is unexpected (not a clean local disconnect)."""
    if reason_code is None:
        return False
    text = str(reason_code)
    if "Normal disconnection" in text:
        return False
    if getattr(reason_code, "is_failure", None) is not None:
        return bool(reason_code.is_failure)
    if isinstance(reason_code, int):
        return reason_code != 0
    return True


def publish_on_client(
    client: mqtt.Client,
    topic: str,
    payload: dict,
    qos: int = 1,
    *,
    retain: bool = False,
) -> bool:
    """Publish on an already-connected client (prefer this in mqtt_worker)."""
    try:
        info = client.publish(topic, json.dumps(payload), qos=qos, retain=retain)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.error("MQTT publish to %s failed: rc=%s", topic, info.rc)
            return False
        return True
    except Exception:
        logger.exception("Failed to publish MQTT message to %s", topic)
        return False


def publish_once(
    topic: str, payload: dict, qos: int = 1, *, retain: bool = False
) -> bool:
    """Publish a single message with a short-lived client. Returns False if skipped/failed.

    Prefer publish_on_client() from the long-lived mqtt_worker so we do not open a
    new TLS session per message (that blocks the worker loop and causes keep-alive
    timeouts).

    Note: AWS IoT does not deliver retained messages to wildcard subscriptions, so
    device config uses retain=False and the gateway online sync re-pushes configs.
    """
    if not mqtt_configured():
        logger.warning("MQTT not configured; skipped publish to %s", topic)
        return False

    # Unique client id avoids clashes when several publishes overlap.
    client = create_mqtt_client(
        client_id=f"{settings.MQTT_CLIENT_ID}-pub-{uuid.uuid4().hex[:8]}"
    )
    connected = threading.Event()
    connect_error: list[object] = []

    def on_connect(client, userdata, flags, reason_code, properties=None):
        failed = bool(getattr(reason_code, "is_failure", False))
        if isinstance(reason_code, int):
            failed = reason_code != 0
        if failed:
            connect_error.append(reason_code)
            connected.set()
            return
        connected.set()

    def on_disconnect(client, userdata, flags, reason_code, properties=None):
        if _disconnect_is_failure(reason_code):
            logger.warning(
                "MQTT disconnected while publishing to %s: %s", topic, reason_code
            )

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect

    try:
        client.connect(settings.MQTT_HOST, settings.MQTT_PORT)
        client.loop_start()
        if not connected.wait(timeout=10):
            logger.error("MQTT connect timed out before publish to %s", topic)
            return False
        if connect_error:
            logger.error(
                "MQTT connect failed before publish to %s: %s",
                topic,
                connect_error[0],
            )
            return False

        info = client.publish(topic, json.dumps(payload), qos=qos, retain=retain)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.error("MQTT publish to %s failed immediately: rc=%s", topic, info.rc)
            return False
        # paho may return None from wait_for_publish; trust is_published().
        info.wait_for_publish(timeout=10)
        if not info.is_published():
            logger.error(
                "MQTT publish to %s timed out (often AWS IoT policy deny on "
                "iot:Publish for this topic, or client disconnect). "
                "Check the cloud Thing policy allows Publish on %s",
                topic,
                topic,
            )
            return False
        return True
    except Exception:
        logger.exception("Failed to publish MQTT message to %s", topic)
        return False
    finally:
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass


def _publish(
    topic: str,
    payload: dict,
    *,
    client: mqtt.Client | None = None,
) -> bool:
    if client is not None:
        return publish_on_client(client, topic, payload)
    return publish_once(topic, payload)


def publish_site_device_configs(
    site_id: int, *, client: mqtt.Client | None = None
) -> tuple[int, int]:
    """Re-publish all devices for a site. Returns (ok_count, fail_count)."""
    from sites.models import Device

    ok = fail = 0
    for device in Device.objects.filter(site_id=site_id):
        if _publish(device.get_config_topic(), device.to_mqtt_config(), client=client):
            ok += 1
        else:
            fail += 1
    return ok, fail


def publish_all_site_device_configs(
    *, client: mqtt.Client | None = None
) -> tuple[int, int]:
    """Re-publish devices for every site (used when mqtt_worker reconnects)."""
    from sites.models import Site

    ok = fail = 0
    for site in Site.objects.all().only("pk"):
        site_ok, site_fail = publish_site_device_configs(site.pk, client=client)
        ok += site_ok
        fail += site_fail
    return ok, fail


def publish_gateway_ack(
    site_id: int,
    *,
    ok: bool,
    site_name: str = "",
    error: str = "",
    client: mqtt.Client | None = None,
) -> bool:
    """Tell the edge gateway whether SITE_ID is valid."""
    payload: dict = {"ok": ok, "site_id": site_id}
    if ok:
        payload["site_name"] = site_name
    if error:
        payload["error"] = error
    return _publish(f"edge/{site_id}/gateway/ack", payload, client=client)


def should_handle_gateway_online(site_id: int) -> bool:
    """Return False if this site was handled very recently (edge online retries)."""
    now = time.monotonic()
    last = _last_gateway_online.get(site_id)
    if last is not None and (now - last) < _GATEWAY_ONLINE_DEDUP_SECONDS:
        return False
    _last_gateway_online[site_id] = now
    return True
