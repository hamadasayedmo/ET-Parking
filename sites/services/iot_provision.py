"""Provision AWS IoT Things and device certificates for edge gateways."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from django.conf import settings
from django.utils import timezone

from sites.models import Site

# Public Amazon Root CA 1 (fallback if MQTT_CA_CERT is unset/missing).
_AMAZON_ROOT_CA1_URL = "https://www.amazontrust.com/repository/AmazonRootCA1.pem"


@dataclass(frozen=True)
class ProvisionResult:
    thing_name: str
    certificate_id: str
    certificate_pem: str
    private_key: str
    ca_pem: str
    mqtt_host: str
    mqtt_port: int
    mqtt_client_id: str


def _iot_client():
    return boto3.client("iot", region_name=settings.AWS_REGION)


def _load_ca_pem() -> str:
    candidates = [
        Path(settings.MQTT_CA_CERT) if settings.MQTT_CA_CERT else None,
        Path(settings.BASE_DIR) / "certs" / "AmazonRootCA1.pem",
    ]
    for path in candidates:
        if path is not None and path.is_file():
            return path.read_text(encoding="utf-8")

    import urllib.request

    with urllib.request.urlopen(_AMAZON_ROOT_CA1_URL, timeout=30) as resp:
        return resp.read().decode("utf-8")


def _ensure_thing(client, thing_name: str) -> None:
    try:
        client.create_thing(thingName=thing_name)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise


def _deactivate_certificate(client, certificate_id: str) -> None:
    if not certificate_id:
        return
    try:
        client.update_certificate(certificateId=certificate_id, newStatus="INACTIVE")
    except ClientError:
        # Best-effort: old cert may already be gone or detached.
        pass


def _detach_old_principal(client, thing_name: str, certificate_id: str) -> None:
    if not certificate_id:
        return
    try:
        desc = client.describe_certificate(certificateId=certificate_id)
        cert_arn = desc["certificateDescription"]["certificateArn"]
        client.detach_thing_principal(thingName=thing_name, principal=cert_arn)
        client.detach_policy(policyName=settings.AWS_IOT_POLICY_NAME, target=cert_arn)
    except ClientError:
        pass


def provision_site_certs(site: Site) -> ProvisionResult:
    """Create (or reuse) Thing, mint a new active cert, return PEMs once.

    Private key is only available in this return value — it is not stored.
    """
    if not site.pk:
        raise ValueError("Site must be saved before provisioning")

    policy_name = settings.AWS_IOT_POLICY_NAME
    if not policy_name:
        raise ValueError("AWS_IOT_POLICY_NAME is not configured")

    mqtt_host = settings.AWS_IOT_ENDPOINT or settings.MQTT_HOST
    if not mqtt_host:
        raise ValueError("AWS_IOT_ENDPOINT / MQTT_HOST is not configured")

    thing_name = site.iot_thing_name or site.default_iot_thing_name
    client = _iot_client()

    _ensure_thing(client, thing_name)

    if site.iot_certificate_id:
        _detach_old_principal(client, thing_name, site.iot_certificate_id)
        _deactivate_certificate(client, site.iot_certificate_id)

    created = client.create_keys_and_certificate(setAsActive=True)
    certificate_arn = created["certificateArn"]
    certificate_id = created["certificateId"]
    certificate_pem = created["certificatePem"]
    private_key = created["keyPair"]["PrivateKey"]

    client.attach_thing_principal(thingName=thing_name, principal=certificate_arn)
    client.attach_policy(policyName=policy_name, target=certificate_arn)

    site.iot_thing_name = thing_name
    site.iot_certificate_id = certificate_id
    site.provisioned_at = timezone.now()
    site.save(update_fields=["iot_thing_name", "iot_certificate_id", "provisioned_at"])

    return ProvisionResult(
        thing_name=thing_name,
        certificate_id=certificate_id,
        certificate_pem=certificate_pem,
        private_key=private_key,
        ca_pem=_load_ca_pem(),
        mqtt_host=mqtt_host,
        mqtt_port=int(settings.MQTT_PORT),
        mqtt_client_id=thing_name,
    )
