"""
Reativacao de leads inativos.

Trigger: lead com `next_follow_up <= now` em SQLite, nao finalizado e nao em
modo_mudo. Gera mensagem personalizada via Gemini, envia via UAZAPI, avanca
o estagio e reagenda para o proximo dia. Em `max_stages` finaliza.

A marcacao inicial de `next_follow_up` deve ser feita pelo consumer (ou por
um job separado) quando o lead fica inativo por `inactive_hours`. Esta etapa
e tratada no proprio run() — calculamos aqui tambem para leads que passaram
`inactive_hours` sem next_follow_up setado.
"""
import logging
from datetime import datetime, timedelta, timezone

from zoneinfo import ZoneInfo

from app import db
from app.client_data import load_client_data
from app.config import settings
from app.services import redis_service as rds, uazapi
from app.services.gemini import generate_reactivation_message

logger = logging.getLogger("followup.reactivation")


def _cfg() -> dict:
    data = load_client_data() or {}
    return (data.get("followups") or {}).get("reactivation") or {}


def _now_tz() -> datetime:
    return datetime.now(ZoneInfo(settings.SCHEDULER_TZ))


async def _seed_inactive_leads(now_tz: datetime, inactive_hours: int) -> None:
    """
    Marca next_follow_up = now para leads com last_customer_message_at antigo
    (sem next_follow_up agendado) — assim o loop abaixo os captura.
    """
    now_utc = now_tz.astimezone(timezone.utc)
    cutoff = (now_utc - timedelta(hours=inactive_hours)).isoformat()
    now_iso = now_utc.isoformat()

    import aiosqlite
    async with aiosqlite.connect(settings.SQLITE_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            """
            SELECT l.phone FROM leads l
            WHERE l.next_follow_up IS NULL
              AND l.last_customer_message_at IS NOT NULL
              AND l.last_customer_message_at <= ?
              AND COALESCE(l.status_conversa, '') NOT IN ('finalizado', 'agendado')
              AND COALESCE(l.modo_mudo, 0) = 0
              AND COALESCE(l.stage_follow_up, 0) = 0
              AND NOT EXISTS (
                  SELECT 1 FROM appointments a
                  WHERE a.phone = l.phone
                    AND a.scheduled_at >= ?
                    AND a.status IN ('booked', 'reminded')
              )
            """,
            (cutoff, now_iso),
        )
        rows = await cur.fetchall()

    for row in rows:
        await db.schedule_followup(
            row["phone"],
            next_follow_up_iso=now_utc.isoformat(),
            stage=1,
        )
    if rows:
        logger.info("reactivation: %d lead(s) sementeados para reativacao", len(rows))


async def run() -> None:
    cfg = _cfg()
    if not cfg.get("enabled", False):
        return

    inactive_hours = int(cfg.get("inactive_hours", 24))
    max_stages = int(cfg.get("max_stages", 3))

    now_tz = _now_tz()
    now_utc_iso = now_tz.astimezone(timezone.utc).isoformat()

    await _seed_inactive_leads(now_tz, inactive_hours)

    due = await db.get_followups_due(now_utc_iso)
    if not due:
        return

    logger.info("reactivation: %d lead(s) devido(s)", len(due))

    for lead in due:
        phone = lead["phone"]
        nome = lead.get("nome") or ""
        stage = int(lead.get("stage_follow_up") or 1)

        if stage > max_stages:
            await db.mark_finalizado(phone)
            continue

        # Atendente humano assumiu: bloqueio ativo no Redis (ate amanha 08:00 SP).
        # Nao reativa por cima do humano — sai sem avancar o estagio, retoma no
        # proximo run apos o bloqueio expirar.
        if await rds.is_blocked(phone):
            logger.info("[%s] bloqueado (humano assumiu) — reativacao adiada", phone)
            continue

        # Trava distribuida: impede que duas execucoes concorrentes do
        # scheduler (rolling update / overlap) gerem dois envios pro mesmo
        # lead. TTL=3600s cobre o pior cenario de Gemini lento + falha.
        if not await rds.acquire_followup_lock(phone, ttl=3600):
            logger.info("[%s] follow-up ja em andamento, pulando", phone)
            continue

        now_str = now_tz.strftime("%A, %d/%m/%Y %H:%M")
        try:
            msg = await generate_reactivation_message(phone, nome, stage, now_str)
        except Exception:
            logger.exception("[%s] falha ao gerar reativacao", phone)
            # Adia 1h para nao reentrar a cada 15min enquanto Gemini esta fora.
            retry_iso = (now_tz + timedelta(hours=1)).astimezone(timezone.utc).isoformat()
            await db.schedule_followup(phone, next_follow_up_iso=retry_iso, stage=stage)
            await rds.release_followup_lock(phone)
            continue

        if not msg:
            logger.info("[%s] mensagem vazia (stage=%d), pulando", phone, stage)
            retry_iso = (now_tz + timedelta(hours=1)).astimezone(timezone.utc).isoformat()
            await db.schedule_followup(phone, next_follow_up_iso=retry_iso, stage=stage)
            await rds.release_followup_lock(phone)
            continue

        if settings.FOLLOWUP_DRY_RUN:
            logger.info("[DRY_RUN][%s] stage=%d -> %s", phone, stage, msg[:160])
        else:
            try:
                await uazapi.send_text(phone, msg)
            except Exception:
                logger.exception("[%s] falha ao enviar reativacao", phone)
                # Adia 1h para nao tentar reenviar a cada 15min (ex.: UAZAPI fora
                # ou token stale ficava em loop infinito).
                retry_iso = (now_tz + timedelta(hours=1)).astimezone(timezone.utc).isoformat()
                await db.schedule_followup(phone, next_follow_up_iso=retry_iso, stage=stage)
                await rds.release_followup_lock(phone)
                continue

        finalize = stage >= max_stages
        new_stage = stage + 1 if not finalize else max_stages
        next_iso = None
        if not finalize:
            next_iso = (now_tz + timedelta(days=1)).astimezone(timezone.utc).isoformat()

        await db.advance_followup_stage(phone, new_stage, next_iso, finalize)
        await rds.release_followup_lock(phone)
        logger.info("[%s] stage %d -> %d (finalize=%s)", phone, stage, new_stage, finalize)
