"""
Google Calendar do bot — dois modos de autenticacao, nesta ordem:

1. OAuth do CLIENTE via SAI Comercial (token broker): quando o snapshot do
   painel traz googleCalendar.connected=true, o bot troca o ingestSecret por um
   access token efemero em POST {SAI_BASE_URL}/api/assistant/google-token/by-slug/{slug}
   e agenda NA AGENDA DO CLIENTE. O access token e cacheado em Redis
   (sai:gcal_token:{slug}) por expires_in - 10min. O refresh token NUNCA chega
   ao bot — quem renova e o SAI.
2. Fallback legado: Service Account via GOOGLE_CREDENTIALS_JSON.

Funcoes publicas:
- list_free_slots(day, duration_minutes) -> list[datetime]
- create_event(phone, nome, start_at, duration_minutes, modalidade) -> event_id
- get_upcoming_events(until) -> list[dict]

O `calendar_id`: snapshot googleCalendar.calendarId (modo OAuth) -> client.yaml
(appointments.google_calendar.calendar_id) -> settings.GOOGLE_CALENDAR_ID -> "primary".
"""
import json
import logging
from datetime import datetime, time, timedelta, timezone
from typing import Optional

import httpx

from app.client_data import load_client_data
from app.config import settings
from app.services import sai_sync

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Margem de seguranca sobre o expires_in do broker (access token dura ~1h;
# cacheamos por ~50min para nao usar token na iminencia de expirar).
BROKER_TTL_MARGIN_SECONDS = 600

_sa_service = None  # cache do modo Service Account
_oauth_service = None  # cache do modo OAuth (token broker)
_oauth_token: Optional[str] = None


def _broker_key(slug: str) -> str:
    return f"sai:gcal_token:{slug}"


def _gcal_cfg() -> dict:
    """Bloco googleCalendar do snapshot do painel ({} se nao houver)."""
    snap = sai_sync.load_snapshot_sync() or {}
    return snap.get("googleCalendar") or {}


def _fetch_broker_token() -> Optional[dict]:
    """Access token do broker do SAI. Tenta o cache Redis; miss -> POST no SAI."""
    cfg = sai_sync._active_config_sync()
    if not cfg:
        return None
    slug, secret = cfg
    client = sai_sync._get_sync_client()
    key = _broker_key(slug)
    if client is not None:
        try:
            raw = client.get(key)
            if raw:
                return json.loads(raw)
        except Exception as exc:
            logger.warning("Calendar broker: leitura do cache falhou: %s", exc)

    url = f"{settings.SAI_BASE_URL.rstrip('/')}/api/assistant/google-token/by-slug/{slug}"
    try:
        res = httpx.post(url, headers={"x-ingest-secret": secret}, timeout=10)
    except Exception as exc:
        logger.warning("Calendar broker: POST %s falhou: %s", url, exc)
        return None
    if res.status_code == 404:
        logger.info("Calendar broker: agenda nao conectada no painel (404)")
        return None
    if res.status_code == 409:
        logger.warning("Calendar broker: acesso revogado — cliente precisa reconectar no painel (409)")
        return None
    if res.status_code != 200:
        logger.warning("Calendar broker: POST %s -> %s: %s", url, res.status_code, res.text[:200])
        return None
    try:
        data = res.json()
    except Exception:
        return None
    token = data.get("access_token")
    if not token:
        return None
    payload = {
        "access_token": token,
        "calendar_id": data.get("calendar_id") or "primary",
        "account_email": data.get("account_email"),
    }
    ttl = max(int(data.get("expires_in") or 3600) - BROKER_TTL_MARGIN_SECONDS, 60)
    if client is not None:
        try:
            client.set(key, json.dumps(payload), ex=ttl)
        except Exception as exc:
            logger.warning("Calendar broker: escrita do cache falhou: %s", exc)
    return payload


def _invalidate_broker_token() -> None:
    """Descarta o access token cacheado (ex.: Google respondeu 401 antes do TTL)."""
    global _oauth_service, _oauth_token
    _oauth_service = None
    _oauth_token = None
    cfg = sai_sync._active_config_sync()
    if not cfg:
        return
    client = sai_sync._get_sync_client()
    if client is not None:
        try:
            client.delete(_broker_key(cfg[0]))
        except Exception:
            pass


def _get_calendar_id() -> str:
    gcal = _gcal_cfg()
    if gcal.get("connected") and gcal.get("calendarId"):
        return gcal["calendarId"]
    data = load_client_data()
    gc = (data.get("appointments") or {}).get("google_calendar") or {}
    return gc.get("calendar_id") or settings.GOOGLE_CALENDAR_ID or "primary"


def _get_oauth_service():
    global _oauth_service, _oauth_token
    tok = _fetch_broker_token()
    if not tok:
        return None
    access_token = tok["access_token"]
    if _oauth_service is not None and _oauth_token == access_token:
        return _oauth_service
    try:
        from google.oauth2.credentials import Credentials as OAuthCredentials
        from googleapiclient.discovery import build

        # Sem refresh token de proposito: quando expirar, o broker renova.
        creds = OAuthCredentials(token=access_token)
        _oauth_service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        _oauth_token = access_token
        logger.info(
            "Google Calendar conectado via SAI (conta %s)", tok.get("account_email") or "?"
        )
    except Exception:
        logger.exception("Erro ao montar Calendar via broker")
        _oauth_service = None
        _oauth_token = None
        return None
    return _oauth_service


def _get_sa_service():
    global _sa_service
    if _sa_service is not None:
        return _sa_service

    if not settings.GOOGLE_CREDENTIALS_JSON:
        logger.warning("Google Calendar nao configurado (GOOGLE_CREDENTIALS_JSON ausente)")
        return None

    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        creds_info = json.loads(settings.GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
        _sa_service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        logger.info("Google Calendar conectado (service account)")
    except Exception:
        logger.exception("Erro ao conectar Google Calendar")
        return None

    return _sa_service


def _get_service():
    """Modo OAuth do cliente (snapshot connected) com fallback no service account."""
    if _gcal_cfg().get("connected"):
        service = _get_oauth_service()
        if service is not None:
            return service
        # Broker indisponivel agora — tenta o fallback legado (se configurado).
    return _get_sa_service()


def _is_unauthorized(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "resp", None), "status", None)
    try:
        return int(status) == 401
    except (TypeError, ValueError):
        return False


def _call_api(make_request):
    """Executa uma chamada ao Calendar. Em modo broker, um 401 (access token
    morreu antes do TTL do cache) invalida o cache e repete UMA vez com token
    novo. Devolve None se nao ha service configurado; propaga outros erros."""
    service = _get_service()
    if service is None:
        return None
    try:
        return make_request(service)
    except Exception as exc:
        if _is_unauthorized(exc) and _gcal_cfg().get("connected"):
            logger.info("Calendar: 401 em modo broker — renovando access token")
            _invalidate_broker_token()
            service = _get_service()
            if service is not None:
                return make_request(service)
        raise


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
    ranges = _ranges_for_day(day)
    if not ranges:
        return []

    day_start = datetime.combine(day.date(), time(0, 0), tzinfo=day.tzinfo or timezone.utc)
    day_end = day_start + timedelta(days=1)

    try:
        result = _call_api(
            lambda s: s.events()
            .list(
                calendarId=_get_calendar_id(),
                timeMin=day_start.isoformat(),
                timeMax=day_end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
    except Exception:
        logger.exception("Erro ao listar eventos")
        return []
    if result is None:
        return []
    events = result.get("items", [])

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
        ev = _call_api(
            lambda s: s.events().insert(calendarId=_get_calendar_id(), body=body).execute()
        )
    except Exception:
        logger.exception("Erro ao criar evento no Calendar")
        return None
    if ev is None:
        return None
    event_id = ev.get("id")
    logger.info("Calendar: evento %s criado para %s em %s", event_id, phone, start_at.isoformat())
    return event_id


async def get_upcoming_events(until: datetime) -> list[dict]:
    """Eventos entre agora e `until` (UTC-aware)."""
    now = datetime.now(timezone.utc)
    try:
        items = _call_api(
            lambda s: s.events()
            .list(
                calendarId=_get_calendar_id(),
                timeMin=now.isoformat(),
                timeMax=until.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
    except Exception:
        logger.exception("Erro ao buscar eventos proximos")
        return []
    if items is None:
        return []
    return items.get("items", [])
