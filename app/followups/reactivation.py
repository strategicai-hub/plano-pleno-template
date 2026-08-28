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
from app.services import nomes, redis_service as rds, sai_sync, uazapi
from app.services.gemini import generate_reactivation_message

logger = logging.getLogger("followup.reactivation")

# Idade maxima aceitavel da lista de pausados do SAI para que o follow-up possa
# disparar. O painel reconcilia a cada 15 min (POLL_INTERVAL_SECONDS): 45 min
# tolera duas falhas seguidas antes de calar a cobranca.
RECONCILE_MAX_AGE_SECONDS = 45 * 60


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
            f"""
            SELECT l.phone FROM leads l
            WHERE l.next_follow_up IS NULL
              AND l.last_customer_message_at IS NOT NULL
              AND l.last_customer_message_at <= ?
              AND COALESCE(l.status_conversa, '') NOT IN ('finalizado', 'agendado')
              AND COALESCE(l.modo_mudo, 0) = 0
              AND COALESCE(l.stage_follow_up, 0) = 0
              {db.FOLLOWUP_HOLD_CLAUSE}
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


async def _sai_state_is_fresh() -> bool:
    """A lista de conversas em atendimento humano esta atualizada?

    O SAI e a fonte da verdade sobre quem tem gente de verdade na conversa, e
    quem a reconcilia e a API (polling de 15 min) — este job roda em OUTRO
    processo (scheduler) e enxerga o resultado pelo Redis/SQLite compartilhados.
    Se a API esta fora, se o SAI esta fora ou se o Redis foi limpo, o scheduler
    seguiria disparando com uma foto velha de quem esta pausado e cobraria lead
    em pleno atendimento — foi essa a janela que sobrou depois de o bloqueio e o
    `modo_mudo` cobrirem o caminho normal.

    Sem vinculo com o SAI o gate nao se aplica (bot autonomo).
    """
    if not await sai_sync.sai_integration_active():
        return True
    idade = await sai_sync.seconds_since_reconcile()
    if idade is None:
        logger.warning(
            "reactivation: lista de pausados do SAI ainda nao conferida neste Redis — "
            "nenhum follow-up sai ate a primeira reconciliacao"
        )
        return False
    if idade > RECONCILE_MAX_AGE_SECONDS:
        logger.warning(
            "reactivation: lista de pausados do SAI conferida ha %.0f min (limite %.0f) — "
            "follow-up suspenso ate a sincronizacao voltar",
            idade / 60, RECONCILE_MAX_AGE_SECONDS / 60,
        )
        return False
    return True


async def run() -> None:
    cfg = _cfg()
    if not cfg.get("enabled", False):
        return

    if not await _sai_state_is_fresh():
        return

    inactive_hours = int(cfg.get("inactive_hours", 24))
    max_stages = int(cfg.get("max_stages", 3))
    # Teto de envios por execucao. Ao ligar a reativacao num bot com base
    # acumulada, TODOS os leads inativos ficam devidos no mesmo ciclo — sem
    # teto isso vira centenas de mensagens em minutos (risco de ban no
    # WhatsApp). Com teto, o backlog escoa aos poucos. 0 = sem limite.
    max_per_run = int(cfg.get("max_per_run", 20))

    now_tz = _now_tz()
    now_utc_iso = now_tz.astimezone(timezone.utc).isoformat()

    await _seed_inactive_leads(now_tz, inactive_hours)

    due = await db.get_followups_due(now_utc_iso)
    if not due:
        return

    total_due = len(due)
    if max_per_run > 0 and total_due > max_per_run:
        due = due[:max_per_run]
        logger.info(
            "reactivation: %d lead(s) devido(s), processando %d neste ciclo (teto max_per_run)",
            total_due, max_per_run,
        )
    else:
        logger.info("reactivation: %d lead(s) devido(s)", total_due)

    for lead in due:
        phone = lead["phone"]
        # Nunca ler `lead["nome"]` cru: a reativacao ja saiu chamando lead pelo
        # nome do perfil do WhatsApp ("Oi Tutu"). nomes.nome_do_lead aplica a
        # precedencia confirmado > cadastro e devolve "" quando nao ha nome
        # confiavel — nesse caso a mensagem sai sem vocativo.
        nome = nomes.nome_do_lead(lead)
        stage = int(lead.get("stage_follow_up") or 1)

        if stage > max_stages:
            await db.mark_finalizado(phone)
            continue

        # Bloqueio ativo: ou o atendente humano assumiu (expira amanha 08:00 SP),
        # ou o operador clicou "Desativar assistente" no SAI (permanente, so sai
        # com "Ativar assistente"). Em nenhum dos dois o bot pode enviar nada —
        # sai sem avancar o estagio e retoma quando o bloqueio cair.
        # `is_blocked` varre as duas formas do numero (com/sem o 9o digito): o
        # SAI bloqueia pelo telefone canonico e aqui o phone vem do SQLite, que
        # o indexa pelo JID da UAZAPI. Casar so a string exata ja deixou
        # follow-up sair depois do "Desativar assistente".
        if await rds.is_blocked(phone):
            motivo = "assistente desativado no SAI" if await rds.is_permanently_blocked(phone) \
                else "humano assumiu"
            logger.info("[%s] bloqueado (%s) — reativacao adiada", phone, motivo)
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
        # Texto no log de proposito: a mensagem e gerada na hora e nao fica
        # gravada em lugar nenhum — sem isso nao da para auditar o que o bot
        # mandou em cada follow-up.
        logger.info(
            "[%s] ENVIADO stage=%d nome=%r texto=%r (proximo=%s finalize=%s)",
            phone, stage, nome, msg, next_iso or "-", finalize,
        )
