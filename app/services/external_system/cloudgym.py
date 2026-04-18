"""
Driver CloudGym — esqueleto enxuto. A implementacao completa (auth v1/v2,
endpoints de classes, attendance, customer) deve ser portada do projeto
`strategicai-hub/seven` conforme o cliente for onboard.

Credenciais via .env: CLOUDGYM_USERNAME, CLOUDGYM_PASSWORD, CLOUDGYM_UNIT_ID,
CLOUDGYM_PROXY (opcional).
"""
import logging
from datetime import datetime
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


def _configured() -> bool:
    return bool(settings.CLOUDGYM_USERNAME and settings.CLOUDGYM_PASSWORD)


async def list_free_slots(day: datetime, duration_minutes: int) -> list[datetime]:
    if not _configured():
        logger.warning("CloudGym nao configurado (CLOUDGYM_USERNAME/PASSWORD ausentes)")
        return []
    # TODO: portar /config/classes/{unit} + /admin/classattendancelist do seven
    logger.info("cloudgym.list_free_slots stub — retornando vazio")
    return []


async def book_appointment(
    phone: str,
    nome: str,
    start_at: datetime,
    duration_minutes: int,
    modalidade: Optional[str] = None,
) -> Optional[str]:
    if not _configured():
        logger.warning("CloudGym nao configurado (CLOUDGYM_USERNAME/PASSWORD ausentes)")
        return None
    # TODO: portar /v1/classattendance + /customer do seven
    logger.info(
        "cloudgym.book_appointment stub — phone=%s start=%s modalidade=%s",
        phone, start_at.isoformat(), modalidade,
    )
    return None


async def get_upcoming_appointments(until: datetime) -> list[dict]:
    if not _configured():
        return []
    # TODO: portar listagem de agendamentos futuros do seven
    return []
