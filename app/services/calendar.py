"""
Facade de agendamento: decide entre Google Calendar e sistema externo
conforme `client.yaml > appointments.source`.

Mantém a superficie estavel para o consumer e para os jobs de follow-up.
"""
import logging
from datetime import datetime
from typing import Optional

from app.client_data import load_client_data
from app.services import calendar_google
from app.services.external_system import get_driver

logger = logging.getLogger(__name__)


def _source() -> str:
    data = load_client_data()
    return ((data.get("appointments") or {}).get("source") or "google_calendar").lower()


def _slot_duration() -> int:
    data = load_client_data()
    return int((data.get("appointments") or {}).get("slot_duration_minutes") or 60)


async def list_free_slots(day: datetime, duration_minutes: Optional[int] = None) -> list[datetime]:
    duration_minutes = duration_minutes or _slot_duration()
    if _source() == "external_system":
        return await get_driver().list_free_slots(day, duration_minutes)
    return await calendar_google.list_free_slots(day, duration_minutes)


async def create_event(
    phone: str,
    nome: str,
    start_at: datetime,
    duration_minutes: Optional[int] = None,
    modalidade: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """Retorna (source, external_id). source = 'google_calendar' | 'external_system'."""
    duration_minutes = duration_minutes or _slot_duration()
    src = _source()
    if src == "external_system":
        ext_id = await get_driver().book_appointment(
            phone, nome, start_at, duration_minutes, modalidade
        )
        return ("external_system", ext_id)
    ext_id = await calendar_google.create_event(
        phone, nome, start_at, duration_minutes, modalidade
    )
    return ("google_calendar", ext_id)


async def get_upcoming_events(until: datetime) -> list[dict]:
    if _source() == "external_system":
        return await get_driver().get_upcoming_appointments(until)
    return await calendar_google.get_upcoming_events(until)
