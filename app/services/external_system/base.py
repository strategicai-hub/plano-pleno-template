"""
Interface que todo driver de sistema externo deve oferecer.

Qualquer modulo em app/services/external_system/ que exponha essas funcoes
pode ser usado como driver — ver __init__.py para registro.
"""
from datetime import datetime
from typing import Optional, Protocol


class ExternalSystemDriver(Protocol):
    async def list_free_slots(
        self, day: datetime, duration_minutes: int
    ) -> list[datetime]:
        ...

    async def book_appointment(
        self,
        phone: str,
        nome: str,
        start_at: datetime,
        duration_minutes: int,
        modalidade: Optional[str] = None,
    ) -> Optional[str]:
        """Retorna o id externo do agendamento (ou None em caso de falha)."""
        ...

    async def get_upcoming_appointments(
        self, until: datetime
    ) -> list[dict]:
        """Retorna appointments com pelo menos: phone, scheduled_at (datetime), external_id."""
        ...
