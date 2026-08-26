import json
import threading

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from parking.mqtt import create_mqtt_client, mqtt_configured
from parking.services import authorize_plate
from sites.models import Device


class Command(BaseCommand):
    help = "Starts the MQTT subscriber worker for gate events"

    def handle(self, *args, **options):
        if not mqtt_configured():
            self.stderr.write(
                "MQTT is not configured. Set MQTT_HOST and certificate paths in .env"
            )
            return

        self.stdout.write("Initializing MQTT Worker...")

        # Use a dedicated client id so this process does not fight AWS console
        # tests or other tools that connect as MQTT_CLIENT_ID (Thing name).
        client_id = f"{settings.MQTT_CLIENT_ID}-sub"
        self.stdout.write(f"MQTT client_id={client_id}")
        client = create_mqtt_client(client_id=client_id)
        client.on_connect = self.on_connect
        client.on_message = self.on_message
        client.on_disconnect = self.on_disconnect
        client.on_subscribe = self.on_subscribe

        client.connect(settings.MQTT_HOST, settings.MQTT_PORT)
        self.stdout.write("Worker started. Listening for events...")
        client.loop_forever()

    def on_connect(self, client, userdata, flags, reason_code, properties=None):
        failed = bool(getattr(reason_code, "is_failure", False))
        if isinstance(reason_code, int):
            failed = reason_code != 0
        if failed:
            self.stderr.write(f"Connect failed: {reason_code}")
            return

        self.stdout.write(f"Connected to MQTT broker ({reason_code})")
        result, mid = client.subscribe("edge/+/cameras/+/events", qos=1)
        self.stdout.write(f"Subscribe edge/+/cameras/+/events -> rc={result} mid={mid}")
        result2, mid2 = client.subscribe("edge/+/gateway/online", qos=1)
        self.stdout.write(f"Subscribe edge/+/gateway/online -> rc={result2} mid={mid2}")

        # Republish on a background thread so we never block the MQTT network
        # loop (blocking here caused keep-alive timeouts).
        threading.Thread(
            target=self._republish_all_configs,
            args=(client,),
            daemon=True,
            name="mqtt-republish-on-connect",
        ).start()

    def _republish_all_configs(self, client):
        close_old_connections()
        try:
            from parking.mqtt import publish_all_site_device_configs

            ok, fail = publish_all_site_device_configs(client=client)
            self.stdout.write(
                f"Republished all site configs on connect ok={ok} fail={fail}"
            )
        except Exception as exc:
            self.stderr.write(f"Republish on connect failed: {exc}")
        finally:
            close_old_connections()

    def on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.stderr.write(f"Disconnected: {reason_code}")

    def on_subscribe(self, client, userdata, mid, reason_codes, properties=None):
        self.stdout.write(f"Subscribe acknowledged mid={mid} codes={reason_codes}")

    def on_message(self, client, userdata, msg):
        close_old_connections()
        try:
            parts = msg.topic.split("/")
            # edge/{site_id}/gateway/online
            if (
                len(parts) == 4
                and parts[0] == "edge"
                and parts[2] == "gateway"
                and parts[3] == "online"
            ):
                # Handle off the network loop — may hit DB + several publishes.
                threading.Thread(
                    target=self._handle_gateway_online,
                    args=(client, parts[1]),
                    daemon=True,
                    name="mqtt-gateway-online",
                ).start()
                return

            if (
                len(parts) != 5
                or parts[0] != "edge"
                or parts[2] != "cameras"
                or parts[4] != "events"
            ):
                self.stderr.write(f"Unexpected topic: {msg.topic}")
                return

            site_id, device_id = parts[1], parts[3]
            payload = json.loads(msg.payload.decode())
            from wallets.services import normalize_plate

            plate = normalize_plate(payload.get("plate") or "")
            if not plate:
                return

            try:
                device = Device.objects.select_related("site", "site__project").get(
                    pk=device_id
                )
            except (Device.DoesNotExist, ValueError):
                self.stderr.write(f"Unknown device on topic {msg.topic}")
                return

            if not device.enabled:
                self.stdout.write(f"Ignored disabled device {device_id}")
                return

            if not device.site.is_active:
                self.stdout.write(
                    f"Ignored inactive site {device.site_id} device={device_id}"
                )
                return

            if str(device.site_id) != site_id:
                self.stderr.write(
                    f"Site mismatch topic={site_id} db={device.site_id} device={device_id}"
                )
                return

            from parking.services import pulse_device

            nested = payload.get("event") if isinstance(payload.get("event"), dict) else {}
            vehicle_type = payload.get("vehicle_type") or nested.get("vehicle_type")

            result = authorize_plate(
                device=device,
                plate=plate,
                vehicle_type=vehicle_type,
            )

            if result.granted:
                pulse_device(device, client=client)
                self.stdout.write(f"Pulse sent to {device_id} plate={plate}")
            elif result.access_request is not None:
                self.stdout.write(
                    f"Escalated access_request={result.access_request.pk} "
                    f"{device_id} plate={plate}"
                )
            else:
                self.stdout.write(f"Denied {device_id} plate={plate}")

        except Exception as exc:
            self.stderr.write(f"Error processing message: {exc}")
        finally:
            close_old_connections()

    def _handle_gateway_online(self, client, site_id: str):
        close_old_connections()
        try:
            from parking.mqtt import (
                publish_gateway_ack,
                publish_site_device_configs,
                should_handle_gateway_online,
            )
            from sites.models import Site

            try:
                site_pk = int(site_id)
            except ValueError:
                self.stderr.write(f"Invalid site id in gateway online: {site_id}")
                return

            if not should_handle_gateway_online(site_pk):
                # Edge retries online until it receives ack/configs; skip quietly.
                return

            try:
                site = Site.objects.get(pk=site_pk)
            except Site.DoesNotExist:
                self.stderr.write(
                    f"Unknown SITE_ID={site_pk} from gateway online — "
                    "edge .env SITE_ID must match Django Site primary key"
                )
                publish_gateway_ack(
                    site_pk,
                    ok=False,
                    error="unknown_site_id",
                    client=client,
                )
                return

            # Reuse the worker connection — do not open a new TLS client per topic.
            publish_gateway_ack(site.pk, ok=True, site_name=site.name, client=client)
            ok, fail = publish_site_device_configs(site.pk, client=client)
            self.stdout.write(
                f"Gateway online site={site.pk} ({site.name}): "
                f"republished configs ok={ok} fail={fail}"
            )
        except Exception as exc:
            self.stderr.write(f"Error handling gateway online: {exc}")
        finally:
            close_old_connections()
