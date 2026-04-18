"""
Driver vazio: usado quando `appointments.source = google_calendar` e nao ha
sistema externo. Todas as operacoes retornam estruturas vazias silenciosamente.
"""
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


async def list_free_slots(day: datetime, duration_minutes: int) -> list[datetime]:
    return []


async def book_appointment(
    phone: str,
    nome: str,
    start_at: datetime,
    duration_minutes: int,
    modalidade: Optional[str] = None,
) -> Optional[str]:
    logger.warning(
        "external_system.noop.book_appointment chamado — configure appointments.source ou external_system.type no client.yaml"
    )
    return None


async def get_upcoming_appointments(until: datetime) -> list[dict]:
    return []
