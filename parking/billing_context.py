"""Thread-local billing method for the next Session created from an Event grant."""

from __future__ import annotations

import threading

_local = threading.local()


def set_pending_billing_method(method: str | None) -> None:
    _local.billing_method = method


def pop_pending_billing_method(default: str = "wallet") -> str:
    method = getattr(_local, "billing_method", None)
    _local.billing_method = None
    return method or default
