"""
Google Calendar via Service Account. Reusa GOOGLE_CREDENTIALS_JSON.

Funcoes publicas:
- list_free_slots(day, duration_minutes, business_hours) -> list[datetime]
- create_event(phone, nome, start_at, duration_minutes, modalidade) -> event_id
- get_upcoming_events(until_iso) -> list[dict]

O `calendar_id` vem do client.yaml (appointments.google_calendar.calendar_id)
com fallback para settings.GOOGLE_CALENDAR_ID.
"""
import json
import logging
from datetime import datetime, time, timedelta, timezone
from typing import Optional

from app.client_data import load_client_data
from app.config import settings

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]

_service = None


def _get_calendar_id() -> str:
    data = load_client_data()
    gc = (data.get("appointments") or {}).get("google_calendar") or {}
    return gc.get("calendar_id") or settings.GOOGLE_CALENDAR_ID or "primary"


def _get_service():
    global _service
    if _service is not None:
        return _service

    if not settings.GOOGLE_CREDENTIALS_JSON:
        logger.warning("Google Calendar nao configurado (GOOGLE_CREDENTIALS_JSON ausente)")
        return None

    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        creds_info = json.loads(settings.GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
        _service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        logger.info("Google Calendar conectado")
    except Exception:
        logger.exception("Erro ao conectar Google Calendar")
        return None

    return _service


def _parse_business_hours(ranges: list[str]) -> list[tuple[time, time]]:
    """Ex.: ['06:00-22:00'] -> [(06:00, 22:00)]"""
    out: list[tuple[time, time]] = []
    for r in ranges or []:
        try:
            a, b = r.split("-")
            h1, m1 = a.strip().split(":")
            h2, m2 = b.strip().split(":")
            out.append((time(int(h1), int(m1)), time(int(h2), int(m2))))
        except Exception:
            logger.warning("business_hours inválido: %s", r)
    return out


def _ranges_for_day(day: datetime) -> list[tuple[time, time]]:
    data = load_client_data()
    bh = (data.get("appointments") or {}).get("business_hours") or {}
    wd = day.weekday()  # 0=seg, 6=dom
    if wd <= 4:
        return _parse_business_hours(bh.get("mon_fri") or [])
    if wd == 5:
        return _parse_business_hours(bh.get("sat") or [])
    return _parse_business_hours(bh.get("sun") or [])


async def list_free_slots(day: datetime, duration_minutes: int) -> list[datetime]:
    """Slots livres no dia (datetime aware), respeitando business_hours e eventos ja agendados."""
    service = _get_service()
    if service is None:
        return []

    ranges = _ranges_for_day(day)
    if not ranges:
        return []

    day_start = datetime.combine(day.date(), time(0, 0), tzinfo=day.tzinfo or timezone.utc)
    day_end = day_start + timedelta(days=1)

    try:
        events = service.events().list(
            calendarId=_get_calendar_id(),
            timeMin=day_start.isoformat(),
            timeMax=day_end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute().get("items", [])
    except Exception:
        logger.exception("Erro ao listar eventos")
        return []

    busy: list[tuple[datetime, datetime]] = []
    for ev in events:
        s = ev.get("start", {}).get("dateTime")
        e = ev.get("end", {}).get("dateTime")
        if s and e:
            busy.append((datetime.fromisoformat(s), datetime.fromisoformat(e)))

    slots: list[datetime] = []
    for start_t, end_t in ranges:
        cur = datetime.combine(day.date(), start_t, tzinfo=day_start.tzinfo)
        range_end = datetime.combine(day.date(), end_t, tzinfo=day_start.tzinfo)
        while cur + timedelta(minutes=duration_minutes) <= range_end:
            slot_end = cur + timedelta(minutes=duration_minutes)
            overlap = any(not (slot_end <= b_s or cur >= b_e) for b_s, b_e in busy)
            if not overlap:
                slots.append(cur)
            cur += timedelta(minutes=duration_minutes)
    return slots


async def create_event(
    phone: str,
    nome: str,
    start_at: datetime,
    duration_minutes: int,
    modalidade: Optional[str] = None,
) -> Optional[str]:
    service = _get_service()
    if service is None:
        return None

    end_at = start_at + timedelta(minutes=duration_minutes)
    summary_parts = [nome or "Lead", phone]
    if modalidade:
        summary_parts.append(modalidade)

    body = {
        "summary": " — ".join(summary_parts),
        "description": f"Agendado automaticamente via IA.\nTelefone: {phone}\nModalidade: {modalidade or '-'}",
        "start": {"dateTime": start_at.isoformat()},
        "end": {"dateTime": end_at.isoformat()},
    }
    try:
        ev = service.events().insert(calendarId=_get_calendar_id(), body=body).execute()
        event_id = ev.get("id")
        logger.info("Calendar: evento %s criado para %s em %s", event_id, phone, start_at.isoformat())
        return event_id
    except Exception:
        logger.exception("Erro ao criar evento no Calendar")
        return None


async def get_upcoming_events(until: datetime) -> list[dict]:
    """Eventos entre agora e `until` (UTC-aware)."""
    service = _get_service()
    if service is None:
        return []

    now = datetime.now(timezone.utc)
    try:
        items = service.events().list(
            calendarId=_get_calendar_id(),
            timeMin=now.isoformat(),
            timeMax=until.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute().get("items", [])
        return items
    except Exception:
        logger.exception("Erro ao buscar eventos proximos")
        return []
