"""
Lembrete de agendamento.

Trigger: agendamento com scheduled_at entre agora e (agora + hours_before),
status='booked', reminder_sent_at IS NULL. Envia template configuravel via
UAZAPI e marca reminder_sent_at.

Fonte: tabela `appointments` no SQLite, alimentada tanto por agendamentos
criados via IA (flag [AGENDAR=...]) quanto por importacoes do sistema
externo do cliente (via driver em external_system/).
"""
import logging
from datetime import datetime, timedelta, timezone

from zoneinfo import ZoneInfo

from app import db
from app.client_data import load_client_data
from app.config import settings
from app.followups import templates
from app.services import redis_service as rds, uazapi

logger = logging.getLogger("followup.appointment_reminder")


def _cfg() -> dict:
    data = load_client_data() or {}
    return (data.get("followups") or {}).get("appointment_reminder") or {}


def _now_tz() -> datetime:
    return datetime.now(ZoneInfo(settings.SCHEDULER_TZ))


def _within_business_hours(now_tz: datetime) -> bool:
    data = load_client_data() or {}
    bh = (data.get("appointments") or {}).get("business_hours") or {}
    weekday = now_tz.weekday()  # 0=Mon … 6=Sun
    if weekday < 5:
        ranges = bh.get("mon_fri") or []
    elif weekday == 5:
        ranges = bh.get("sat") or []
    else:
        ranges = bh.get("sun") or []
    if not ranges:
        return False
    current = now_tz.time()
    for r in ranges:
        start_s, end_s = r.split("-")
        start = datetime.strptime(start_s, "%H:%M").time()
        end = datetime.strptime(end_s, "%H:%M").time()
        if start <= current <= end:
            return True
    return False


async def run() -> None:
    cfg = _cfg()
    if not cfg.get("enabled", False):
        return

    hours_before = int(cfg.get("hours_before", 3))
    now_tz = _now_tz()

    if not _within_business_hours(now_tz):
        logger.debug("appointment_reminder: fora do horário comercial, pulando")
        return

    until_tz = now_tz + timedelta(hours=hours_before)

    now_iso = now_tz.astimezone(timezone.utc).isoformat()
    until_iso = until_tz.astimezone(timezone.utc).isoformat()

    due = await db.get_appointments_for_reminder(until_iso=until_iso, now_iso=now_iso)
    if not due:
        return

    logger.info("appointment_reminder: %d agendamento(s) dentro da janela", len(due))

    for appt in due:
        phone = appt["phone"]
        # Atendente humano assumiu: bloqueio ativo no Redis (ate amanha 08:00 SP).
        # Nao envia lembrete por cima do humano — pula sem marcar reminder_sent,
        # retoma no proximo run apos o bloqueio expirar (se ainda na janela).
        if await rds.is_blocked(phone):
            logger.info("[%s] bloqueado (humano assumiu) — lembrete adiado", phone)
            continue
        # Trava idempotente para impedir reenvio em rolling update do scheduler.
        if not await rds.acquire_followup_lock(phone, ttl=3600):
            logger.info("[%s] lembrete ja em andamento, pulando", phone)
            continue
        # Pega nome do lead (SQLite)
        lead = await db.get_lead(phone)
        nome = (lead or {}).get("nome") or ""
        # Formata horario no fuso do scheduler
        try:
            sched_dt = datetime.fromisoformat(appt["scheduled_at"])
            sched_local = sched_dt.astimezone(ZoneInfo(settings.SCHEDULER_TZ))
            horario = sched_local.strftime("%H:%M")
        except Exception:
            horario = ""

        modalidade = appt.get("modalidade") or ""
        msg = templates.get(
            "appointment_reminder",
            nome=nome,
            horario=horario,
            modalidade=modalidade,
        )

        if not msg:
            logger.info("[%s] template appointment_reminder vazio, pulando", phone)
            await rds.release_followup_lock(phone)
            continue

        if settings.FOLLOWUP_DRY_RUN:
            logger.info("[DRY_RUN][%s] appointment_reminder -> %s", phone, msg[:160])
        else:
            try:
                await uazapi.send_text(phone, msg)
            except Exception:
                logger.exception("[%s] falha ao enviar lembrete de agendamento", phone)
                await rds.release_followup_lock(phone)
                continue

        await db.mark_reminder_sent(appt["id"])
        await rds.release_followup_lock(phone)
        logger.info("[%s] reminder enviado (appointment_id=%s)", phone, appt["id"])
