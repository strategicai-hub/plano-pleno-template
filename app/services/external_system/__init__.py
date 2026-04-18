"""
Resolvedor de driver para sistema externo do cliente.

Selecao via client.yaml:
    appointments:
      external_system:
        type: "cloudgym" | "none"

Drivers disponiveis:
- cloudgym: integracao com CloudGym (academias)
- noop: placeholder que nao faz nada (usado quando source=google_calendar)
"""
from typing import Any

from app.client_data import load_client_data

from . import cloudgym, noop
from .base import ExternalSystemDriver

_DRIVERS: dict[str, Any] = {
    "cloudgym": cloudgym,
    "noop": noop,
    "none": noop,
}


def get_driver() -> ExternalSystemDriver:
    data = load_client_data()
    es = (data.get("appointments") or {}).get("external_system") or {}
    kind = (es.get("type") or "none").lower()
    return _DRIVERS.get(kind, noop)
