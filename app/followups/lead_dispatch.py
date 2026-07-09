"""
Disparo de 1o contato para leads recebidos de origem externa (versao generica).

Trigger: rows 'pending' na lead_dispatch_queue (SQLite), alimentada pelo intake
HTTP do SAI Comercial (app/services/lead_intake.py -> POST /sai/leads). Job cron
de 1 minuto registrado em scheduler.py quando client.yaml > lead_dispatch.enabled.

Medidas anti-ban Meta (o numero dispara mensagens frias — texto repetido em
massa e envio em rajada sao os principais gatilhos de bloqueio):
- no maximo 1 envio por execucao do job;
- gate de espacamento aleatorio (chave Redis com TTL), compartilhado com o
  job de reativacao — serializa TODO envio proativo do numero;
- janela de dias/horario configuravel (fora dela a fila espera);
- cap diario de envios;
- texto gerado por IA com temperatura alta (fallback: pool de templates);
- delay/typing nativo da UAZAPI randomizado + presence "composing".

Apos o envio, o historico do lead e semeado no Redis (contexto + mensagem
enviada) para que o fluxo normal do bot continue a qualificacao quando ele
responder, e um follow-up D+1 e agendado na infra de reativacao existente.
"""
import asyncio
import logging
import random
from datetime import datetime, time as dtime, timedelta, timezone

from zoneinfo import ZoneInfo

from app import db
from app.client_data import load_client_data
from app.config import settings
from app.services import lead_intake, redis_service as rds, sai_sync, uazapi
from app.services.gemini import generate_first_contact_message

logger = logging.getLogger("followup.lead_dispatch")

# Fallback quando o Gemini falha/retorna vazio. Sobrescrevivel via
# client.yaml > lead_dispatch.templates. Placeholders: {nome}, {saudacao},
# {assistente}.
FALLBACK_TEMPLATES = [
    "Olá {nome}, {saudacao}! Aqui é {assistente}. Recebi seu contato e queria te ajudar. Posso te fazer algumas perguntas rápidas?",
    "Oi {nome}, {saudacao}! Meu nome é {assistente}. Vi que você deixou seu contato e quero entender melhor o que procura. Podemos conversar?",
    "Olá {nome}, tudo bem? Sou {assistente} e recebi seu interesse. Me conta rapidinho: como posso te ajudar hoje?",
    "Oi {nome}! {saudacao}! Aqui é {assistente}. Recebi sua solicitação e adoraria te atender. Pra começar, me diz o que você está buscando?",
    "Olá {nome}, {saudacao}! Sou {assistente} e vi que você entrou em contato. Vamos bater um papo rápido? O que te trouxe até aqui?",
]

_DAY_IDX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _cfg() -> dict:
    return (load_client_data() or {}).get("lead_dispatch") or {}


def _now_tz() -> datetime:
    return datetime.now(ZoneInfo(settings.SCHEDULER_TZ))


def _parse_days(expr: str) -> set[int]:
    """Parse estilo cron ("mon-fri", "mon,wed,sat") para weekdays 0-6."""
    days: set[int] = set()
    for part in (expr or "").strip().lower().split(","):
        part = part.strip()
        if "-" in part:
            a, _, b = part.partition("-")
            ia, ib = _DAY_IDX.get(a.strip()), _DAY_IDX.get(b.strip())
            if ia is not None and ib is not None:
                if ia <= ib:
                    days.update(range(ia, ib + 1))
                else:  # wrap (ex.: "sat-mon")
                    days.update(range(ia, 7))
                    days.update(range(0, ib + 1))
        elif part in _DAY_IDX:
            days.add(_DAY_IDX[part])
    return days or set(range(0, 5))  # default mon-fri


def _within_window(now_tz: datetime, cfg: dict) -> bool:
    if now_tz.weekday() not in _parse_days(str(cfg.get("days", "mon-fri"))):
        return False
    try:
        start = datetime.strptime(str(cfg.get("hours_start", "08:00")), "%H:%M").time()
        end = datetime.strptime(str(cfg.get("hours_end", "18:00")), "%H:%M").time()
    except ValueError:
        logger.warning("lead_dispatch: hours_start/hours_end invalidos, usando 08:00-18:00")
        start, end = dtime(8, 0), dtime(18, 0)
    return start <= now_tz.time() <= end


def _spacing_seconds(cfg: dict) -> int:
    lo = max(int(cfg.get("spacing_minutes_min", 3)), 1) * 60
    hi = max(int(cfg.get("spacing_minutes_max", 8)), 1) * 60
    if hi < lo:
        lo, hi = hi, lo
    return random.randint(lo, hi)


def spacing_seconds() -> int:
    """Espacamento aleatorio (s) entre envios proativos. Usado tambem pelo
    job de reativacao para compartilhar o mesmo gate anti-ban."""
    return _spacing_seconds(_cfg())


def _saudacao(now_tz: datetime) -> str:
    if now_tz.hour < 12:
        return "bom dia"
    if now_tz.hour < 18:
        return "boa tarde"
    return "boa noite"


def _render_fallback(item: dict, cfg: dict, now_tz: datetime) -> str:
    templates = cfg.get("templates") or FALLBACK_TEMPLATES
    data = load_client_data() or {}
    assistente = ((data.get("assistant") or {}).get("name") or "seu atendente").strip()
    nome = (item.get("nome") or "").strip().split(" ")[0].title() or "tudo bem"
    values = {
        "nome": nome,
        "saudacao": _saudacao(now_tz),
        "assistente": assistente,
    }
    template = random.choice(list(templates))
    try:
        return template.format(**values)
    except (KeyError, IndexError):
        logger.warning("lead_dispatch: template de fallback invalido, usando o padrao")
        return FALLBACK_TEMPLATES[0].format(**values)


async def _already_engaged(variants: set[str]) -> bool:
    """Lead ja conversou com o bot/negocio? (respondeu antes do disparo sair,
    ou ja era lead organico). Checa historico Redis e SQLite nas duas formas
    do numero."""
    for v in variants:
        if await rds.has_chat_history(v):
            return True
        lead = await db.get_lead(v)
        if lead and lead.get("last_customer_message_at"):
            return True
    return False


async def run() -> None:
    cfg = _cfg()
    if not cfg.get("enabled", False):
        return

    now_tz = _now_tz()
    if not _within_window(now_tz, cfg):
        return
    if await rds.is_dispatch_gated():
        return

    daily_cap = int(cfg.get("daily_cap", 60))
    midnight_iso = now_tz.replace(hour=0, minute=0, second=0, microsecond=0) \
        .astimezone(timezone.utc).isoformat()
    sent_today = await db.count_dispatches_sent_since(midnight_iso)
    if sent_today >= daily_cap:
        logger.info("lead_dispatch: cap diario atingido (%d/%d)", sent_today, daily_cap)
        return

    now_utc_iso = now_tz.astimezone(timezone.utc).isoformat()
    candidates = await db.get_pending_dispatches(now_utc_iso, limit=10)
    if not candidates:
        return

    for item in candidates:
        phone = item["phone"]
        variants = lead_intake.phone_variants(phone)

        if await _already_engaged(variants):
            await db.mark_dispatch_skipped(item["id"], "already_engaged")
            logger.info("[%s] lead ja conversou — 1o contato pulado", phone)
            continue

        blocked = False
        for v in variants:
            if await rds.is_blocked(v):
                blocked = True
                break
        if blocked:
            await db.mark_dispatch_skipped(item["id"], "human_active")
            logger.info("[%s] atendimento humano ativo — 1o contato pulado", phone)
            continue

        # Trava distribuida: impede envio duplo em overlap de scheduler
        # (rolling update). TTL curto — o envio leva segundos.
        if not await rds.acquire_followup_lock(phone, ttl=600):
            logger.info("[%s] disparo ja em andamento, pulando", phone)
            continue

        try:
            await _dispatch_one(item, cfg, now_tz)
        except Exception as e:
            logger.exception("[%s] falha no disparo de 1o contato", phone)
            retry_iso = (now_tz + timedelta(minutes=30)).astimezone(timezone.utc).isoformat()
            await db.mark_dispatch_failed(item["id"], str(e), retry_iso)
        finally:
            await rds.release_followup_lock(phone)
        # No maximo 1 tentativa de envio por execucao — o espacamento entre
        # leads vem do gate + cadencia do cron.
        return


async def _dispatch_one(item: dict, cfg: dict, now_tz: datetime) -> None:
    phone = item["phone"]
    nome = (item.get("nome") or "").strip()

    msg = await generate_first_contact_message(
        phone,
        nome,
        observacao=item.get("observacao") or "",
    )
    if not msg:
        msg = _render_fallback(item, cfg, now_tz)

    # Corrida: o lead pode ter mandado mensagem enquanto a IA gerava. Se ja ha
    # historico agora, o fluxo normal do bot assumiu — disparar por cima
    # geraria mensagem dupla/incoerente.
    variants = lead_intake.phone_variants(phone)
    for v in variants:
        if await rds.has_chat_history(v):
            await db.mark_dispatch_skipped(item["id"], "replied_during_generation")
            logger.info("[%s] lead respondeu durante a geracao — disparo cancelado", phone)
            return

    if settings.FOLLOWUP_DRY_RUN:
        logger.info("[DRY_RUN][%s] first_contact -> %s", phone, msg[:200])
        await db.mark_dispatch_skipped(item["id"], "dry_run")
        return

    try:
        await uazapi.send_presence(phone, "composing")
        await asyncio.sleep(2)
    except Exception:
        logger.warning("[%s] send_presence falhou (seguindo com o envio)", phone)
    await uazapi.send_text(phone, msg, delay=random.randint(4000, 9000))

    await db.mark_dispatch_sent(item["id"])

    # Callback ao SAI: leads inseridos manualmente no Painel IA (source_phone
    # "sai:<slug>") migram para "Ja contatados". Fire-and-forget.
    source_phone = str(item.get("source_phone") or "")
    if source_phone.startswith("sai:") and cfg.get("callback_enabled", True):
        try:
            await sai_sync.report_lead_sent(
                external_id=(item.get("external_id") or None),
                phone=phone,
                status="SENT",
            )
        except Exception:
            logger.warning("[%s] report_lead_sent falhou (seguindo)", phone)

    # Seeding do historico: quando o lead responder, o gemini.chat() ve este
    # contexto + a mensagem enviada e continua a qualificacao naturalmente.
    # Semeia nas duas variantes porque o JID da resposta pode vir sem o 9.
    contexto = (
        "[CONTEXTO DO SISTEMA: lead recebido de origem externa. "
        f"Nome: {nome or '-'}. "
        f"Observacao do cadastro: {item.get('observacao') or '-'}. "
        "Voce iniciou o contato com a mensagem a seguir — quando o lead responder, "
        "continue a qualificacao a partir dela, sem se apresentar de novo.]"
    )
    for v in variants:
        await rds.append_chat_history(v, "user", contexto)
        await rds.append_chat_history(v, "model", msg)

    # Painel SAI / CRM Redis + SQLite
    if not await rds.get_lead(phone):
        await rds.create_lead(phone, nome)
    await rds.update_lead(phone, name=nome, status_conversa="Primeiro contato enviado")
    await db.upsert_lead(phone, nome=nome)

    # Follow-up automatico se o lead nao responder: entra no ciclo da
    # reativacao (stages 1..max_stages, 1/dia). Cancelado pelo consumer se o
    # lead responder antes.
    followup_after_hours = int(cfg.get("followup_after_hours", 24))
    if followup_after_hours > 0:
        next_iso = (now_tz + timedelta(hours=followup_after_hours)) \
            .astimezone(timezone.utc).isoformat()
        await db.schedule_followup(phone, next_follow_up_iso=next_iso, stage=1)

    await rds.set_dispatch_gate(_spacing_seconds(cfg))
    logger.info("[%s] 1o contato enviado (nome=%s)", phone, nome or "-")
