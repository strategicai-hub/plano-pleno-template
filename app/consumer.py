"""
Fluxo 2: RabbitMQ -> IA -> Resposta WhatsApp
Consome mensagens da fila, processa com Gemini e responde via UAZAPI.
"""
import asyncio
import json
import logging
import re
import time
from contextvars import ContextVar
from datetime import datetime

import redis as redis_sync

from app import db
from app.config import settings
from app.images import MEDIA_DICT
from app.services import calendar as calendar_facade
from app.services import redis_service as rds
from app.services import uazapi
from app.services.gemini import chat as gemini_chat, transcribe_audio, analyze_image, generate_summary
from app.services.rabbitmq import consume
from app.services.redis_keys import session_log_key
from app.services import sheets_service

logger = logging.getLogger(__name__)

# Tipos de mensagem de texto
TEXT_TYPES = {"ExtendedTextMessage", "Conversation", "ContactMessage", "ReactionMessage"}

# --- LOG DE SESSAO ---
# Buffer por task: cada mensagem processada e suas tasks derivadas compartilham
# uma mesma lista via ContextVar. Tasks criadas com asyncio.create_task herdam
# o contexto automaticamente.
_LOG_KEY = session_log_key()
_session_log_var: ContextVar[list[str]] = ContextVar("session_log")

try:
    _log_redis = redis_sync.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
except Exception:
    _log_redis = None


# ---- formatacao de linhas de log ----

def _msg(text: str) -> str:
    return f'<span style="color:#3498db"><b>📩 MSG</b></span> {text}'

def _ai(text: str) -> str:
    return f'<span style="color:#9b59b6"><b>🤖 IA</b></span> {text}'

def _ok(text: str) -> str:
    return f'<span style="color:#27ae60"><b>✅ OK</b></span> {text}'

def _warn(text: str) -> str:
    return f'<span style="color:#e67e22"><b>⚠️ AVISO</b></span> {text}'

def _err(text: str) -> str:
    return f'<span style="color:#e74c3c"><b>❌ ERRO</b></span> {text}'


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def log(line: str) -> None:
    logger.info(_strip_html(line))
    buf = _session_log_var.get(None)
    if buf is not None:
        buf.append(line)


def _save_session_log(phone: str) -> None:
    global _log_redis
    buf = _session_log_var.get(None)
    if not buf:
        return
    if _log_redis is None:
        try:
            _log_redis = redis_sync.Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
            )
        except Exception as e:
            logger.error("Nao foi possivel conectar ao Redis para logs: %s", e)
    if _log_redis:
        entry = json.dumps(
            {"ts": time.time(), "phone": phone, "lines": list(buf)},
            ensure_ascii=False,
        )
        try:
            _log_redis.lpush(_LOG_KEY, entry)
            _log_redis.ltrim(_LOG_KEY, 0, 499)
        except Exception as e:
            logger.error("Erro ao salvar log no Redis: %s", e)
            _log_redis = None  # forca reconexao na proxima chamada
    buf.clear()


# ---- helpers ----

def _is_group(chat_id: str) -> bool:
    return "@g.us" in chat_id


def _parse_ai_response(text: str) -> tuple[list[dict], bool, bool, tuple[datetime, str] | None]:
    """
    Parseia a resposta da IA:
    - Extrai flag [FINALIZADO=0/1]
    - Extrai flag [TRANSFERIR=0/1] (indica transferencia para equipe humana)
    - Extrai flag [AGENDAR=YYYY-MM-DDTHH:MM|modalidade] (PLENO apenas)
    - Quebra em partes (por \\n\\n ou |||)
    - Detecta tags de midia e substitui pelos links do dicionario
    Retorna (partes, finalizado, transferir, agendar).
    agendar = (datetime, modalidade) quando a IA emitiu [AGENDAR=...]; None caso contrario.
    """
    finalizado = False
    match = re.search(r"\[FINALIZADO=(\d)\]", text)
    if match:
        finalizado = match.group(1) == "1"
        text = re.sub(r"\[FINALIZADO=\d\]", "", text).strip()

    transferir = False
    match_t = re.search(r"\[TRANSFERIR=(\d)\]", text)
    if match_t:
        transferir = match_t.group(1) == "1"
        text = re.sub(r"\[TRANSFERIR=\d\]", "", text).strip()

    agendar: tuple[datetime, str] | None = None
    match_a = re.search(r"\[AGENDAR=([^\]|]+?)(?:\|([^\]]+))?\]", text)
    if match_a:
        iso_str = match_a.group(1).strip()
        modalidade = (match_a.group(2) or "").strip()
        try:
            agendar = (datetime.fromisoformat(iso_str), modalidade)
        except ValueError:
            logger.warning("AGENDAR com formato invalido: %r", iso_str)
        text = re.sub(r"\[AGENDAR=[^\]]+\]", "", text).strip()

    if "|||" in text:
        raw_parts = [p.strip() for p in text.split("|||") if p.strip()]
    else:
        raw_parts = [p.strip() for p in text.split("\n\n") if p.strip()]

    parts = []
    for part in raw_parts:
        tag_match = re.search(r"\[([A-Z_]+)\]", part)
        if tag_match and f"[{tag_match.group(1)}]" in MEDIA_DICT:
            tag = f"[{tag_match.group(1)}]"
            media = MEDIA_DICT[tag]
            parts.append({"type": media["type"], "content": media["url"]})
        else:
            parts.append({"type": "text", "content": part})

    if not parts:
        parts = [{"type": "text", "content": text}]

    return parts, finalizado, transferir, agendar


# ---- processamento principal ----

def _begin_session_log() -> None:
    """Cria um buffer de log isolado para a coroutine atual.

    Tasks filhas herdam este buffer por padrao (asyncio.create_task copia o
    contexto). Se uma task precisa de um buffer independente, chame isso
    dentro dela antes de logar.
    """
    _session_log_var.set([])


async def _process_message(msg: dict) -> None:
    _begin_session_log()
    phone = msg.get("phone", "")
    chat_id = msg.get("chat_id", "")
    from_me = msg.get("from_me", False)
    msg_type = msg.get("msg_type", "")
    msg_text = msg.get("msg", "")
    push_name = msg.get("push_name", "")

    # A) Descarta mensagens invalidas / nao suportadas
    if not phone or msg_type in ("", "Unknown"):
        logger.info("Ignorando mensagem invalida (phone=%r, msg_type=%r)", phone, msg_type)
        return

    # A.1) Whitelist (se configurada, apenas numeros listados recebem resposta)
    allowed = settings.allowed_phones_set
    if allowed and phone not in allowed:
        logger.info("Phone %s fora da whitelist ALLOWED_PHONES - ignorando", phone)
        return

    # B) Mensagem propria -> bloqueia agente por 1h
    if from_me:
        await rds.set_block(phone)
        logger.info("Humano assumiu chat %s - agente bloqueado por 1h", chat_id)
        return

    # C) Verifica bloqueio ativo
    if await rds.is_blocked(phone):
        logger.info("Agente bloqueado para %s - ignorando", chat_id)
        return

    # D) Filtra grupos
    if _is_group(chat_id):
        return

    # D.1) Comando /reset
    if msg_type in TEXT_TYPES and (msg_text or "").strip().lower() == "/reset":
        await rds.clear_chat_history(phone)
        await rds.delete_lead(phone)
        await rds.delete_buffer(phone)
        log(_ok(f"[{phone}] Reset solicitado — historico e lead apagados"))
        try:
            await uazapi.send_text(phone, "Conversa reiniciada.")
        except Exception as e:
            log(_err(f"[{phone}] Falha ao confirmar reset via WhatsApp: {e}"))
            logger.exception("Erro ao confirmar reset para %s", phone)
        _save_session_log(phone)
        return

    # Cadastro de lead
    lead = await rds.get_lead(phone)
    if not lead:
        lead = await rds.create_lead(phone, push_name)

    if push_name and lead.get("name", "") != push_name:
        await rds.update_lead(phone, name=push_name)

    # PLENO: marca ultimo contato do lead no SQLite (insumo do reactivation job)
    try:
        await db.touch_last_message(phone)
        if push_name:
            await db.upsert_lead(phone, nome=push_name)
    except Exception as e:
        log(_warn(f"[{phone}] Falha ao atualizar last_customer_message_at: {e}"))

    # E) Identificacao do tipo de mensagem
    media_url = msg.get("media_url", "")
    if msg_type in TEXT_TYPES:
        buffer_text = msg_text
    elif msg_type == "AudioMessage":
        log(f"[TOOL AUDIO] Executando transcribe_audio(phone={phone})")
        try:
            if media_url:
                audio_bytes = await uazapi.download_media(media_url)
                transcription = await transcribe_audio(audio_bytes)
                buffer_text = f"[Audio transcrito]: {transcription}"
                log(_ok(f"[TOOL AUDIO] Resultado: SUCESSO - audio transcrito para {phone}"))
            else:
                buffer_text = "[Audio recebido - nao foi possivel transcrever]"
                log(_warn(f"[TOOL AUDIO] Resultado: FALHA - sem media_url para {phone}"))
        except Exception as e:
            log(_err(f"[TOOL AUDIO] Resultado: EXCECAO - {e}"))
            logger.exception("Erro ao transcrever audio")
            buffer_text = "[Audio recebido - erro na transcricao]"
    elif msg_type == "ImageMessage":
        log(f"[TOOL IMAGEM] Executando analyze_image(phone={phone})")
        try:
            caption = msg.get("caption", "")
            if media_url:
                image_bytes = await uazapi.download_media(media_url)
                description = await analyze_image(image_bytes)
                buffer_text = f"[Imagem recebida]: {description}"
                if caption:
                    buffer_text += f"\nLegenda: {caption}"
                log(_ok(f"[TOOL IMAGEM] Resultado: SUCESSO - imagem analisada para {phone}"))
            else:
                buffer_text = "[Imagem recebida - nao foi possivel analisar]"
                log(_warn(f"[TOOL IMAGEM] Resultado: FALHA - sem media_url para {phone}"))
        except Exception as e:
            log(_err(f"[TOOL IMAGEM] Resultado: EXCECAO - {e}"))
            logger.exception("Erro ao analisar imagem")
            buffer_text = "[Imagem recebida - erro na analise]"
    else:
        buffer_text = msg_text or f"[Mensagem do tipo {msg_type} recebida]"

    if not buffer_text:
        return

    # F) Buffer de mensagens (debounce)
    count = await rds.push_buffer(phone, buffer_text)

    if count > 1:
        logger.info("Buffer ja ativo para %s (count=%d) - saindo", phone, count)
        return

    if phone not in settings.debounce_bypass_set:
        await asyncio.sleep(settings.DEBOUNCE_SECONDS)

    messages = await rds.get_buffer(phone)
    await rds.delete_buffer(phone)

    unified_msg = "\n".join(messages)
    log(_msg(f"[{phone} - {push_name}] {unified_msg[:300]}"))

    # G) Processamento com IA (com retry)
    log(f"[TOOL GEMINI] Executando chat(phone={phone}, msg_len={len(unified_msg)})")
    ai_response = ""
    last_error = ""
    tokens = (0, 0, 0)
    for attempt in range(6):
        try:
            ai_response, tokens = await gemini_chat(phone, unified_msg, lead.get("name", ""))
        except Exception as e:
            last_error = str(e)
            log(_err(f"[TOOL GEMINI] Tentativa {attempt + 1}/6: FALHA - {e}"))
            logger.exception("Erro no Gemini (tentativa %d)", attempt + 1)

        if ai_response:
            break
        if not last_error:
            log(_warn(f"[TOOL GEMINI] Tentativa {attempt + 1}/6: resposta vazia"))
        await asyncio.sleep(2)

    if not ai_response:
        log(_err(f"[TOOL GEMINI] Resultado: FALHA - vazio apos 6 tentativas. Ultimo erro: {last_error}"))
        _save_session_log(phone)
        return

    # H) Verifica bloqueio pos-IA
    if await rds.is_blocked(phone):
        log(_warn(f"[{phone}] Humano assumiu durante processamento — resposta descartada"))
        _save_session_log(phone)
        return

    # I) Parsing e envio
    parts, finalizado, transferir, agendar = _parse_ai_response(ai_response)
    log(_ok(f"[TOOL GEMINI] Resultado: SUCESSO - {len(parts)} parte(s) gerada(s), finalizado={finalizado}, transferir={transferir}, agendar={agendar is not None}"))
    log(_ai(f"[{phone}] {ai_response[:400]}"))
    if tokens[2]:
        log(f"[TOKENS] Entrada: {tokens[0]} | Sa\u00edda: {tokens[1]} | Total: {tokens[2]}")

    for i, part in enumerate(parts):
        try:
            if part["type"] == "text":
                await uazapi.send_text(phone, part["content"])
            elif part["type"] == "image":
                await uazapi.send_image(phone, part["content"])
                await asyncio.sleep(3)
            elif part["type"] == "document":
                await uazapi.send_document(phone, part["content"])
            elif part["type"] == "video":
                await uazapi.send_video(phone, part["content"])
        except Exception as e:
            log(_err(f"[TOOL WHATSAPP] Resultado: FALHA ao enviar {part['type']} ({i+1}/{len(parts)}) - {e}"))
            logger.exception("Erro ao enviar %s para %s", part["type"], phone)

    # J) Alerta de atendimento humano
    if transferir:
        asyncio.create_task(_maybe_send_alert(phone, lead, unified_msg))

    # J.1) PLENO: cria agendamento quando a IA emitiu [AGENDAR=...]
    if agendar is not None:
        asyncio.create_task(_handle_agendar(phone, lead.get("name", ""), agendar))

    # K) Pos-envio: finalizacao + resumo em background
    if finalizado:
        await rds.set_block(phone)
        await rds.update_lead(phone, status_conversa="Finalizado")
        log(_ok(f"[{phone}] Conversa marcada como finalizada"))

    asyncio.create_task(_update_summary_and_sheets(phone, lead.get("name", "")))

    _save_session_log(phone)


async def _maybe_send_alert(phone: str, lead: dict, user_msg: str) -> None:
    """Envia alerta de atendimento humano. Chamada apenas quando a IA emite [TRANSFERIR=1]."""
    _begin_session_log()
    if not settings.ALERT_PHONE:
        log(_warn(f"[TOOL ALERTA_EQUIPE] Nao acionado - ALERT_PHONE nao configurado"))
        _save_session_log(phone)
        return
    if await rds.is_alert_sent(phone):
        log(f"[TOOL ALERTA_EQUIPE] Ignorado - alerta ja enviado recentemente para {phone}")
        _save_session_log(phone)
        return

    contact = lead.get("name", "") or phone
    motivo = user_msg.strip()[:120] or "Transferencia solicitada pela IA"
    log(f"[TOOL ALERTA_EQUIPE] Executando(phone={phone}, motivo={motivo[:80]})")
    alert_text = (
        f"\U0001f6a8 ATENDIMENTO HUMANO \U0001f6a8\n"
        f"Contato: {contact} ({phone})\n"
        f"Motivo: {motivo}"
    )
    try:
        await uazapi.send_text(settings.ALERT_PHONE, alert_text)
        await rds.set_alert_sent(phone)
        log(_ok(f"[TOOL ALERTA_EQUIPE] Resultado: SUCESSO - equipe notificada sobre {phone}"))
        _save_session_log(phone)
    except Exception as e:
        log(_err(f"[TOOL ALERTA_EQUIPE] Resultado: FALHA - {e}"))
        logger.exception("Erro ao enviar alerta de atendimento humano: %s", e)
        _save_session_log(phone)


async def _handle_agendar(phone: str, nome: str, agendar: tuple[datetime, str]) -> None:
    """PLENO: cria evento no calendario (Google ou externo) e persiste em SQLite."""
    _begin_session_log()
    start_at, modalidade = agendar
    log(f"[TOOL AGENDAR] Executando create_event(phone={phone}, start={start_at.isoformat()}, modalidade={modalidade or '-'})")
    try:
        source, external_id = await calendar_facade.create_event(
            phone=phone,
            nome=nome or phone,
            start_at=start_at,
            modalidade=modalidade or None,
        )
        if not external_id:
            log(_warn(f"[TOOL AGENDAR] Resultado: sem external_id — backend {source} pode estar indisponivel"))
        await db.schedule_appointment(
            phone=phone,
            scheduled_at_iso=start_at.isoformat(),
            source=source,
            external_id=external_id,
            modalidade=modalidade or None,
        )
        log(_ok(f"[TOOL AGENDAR] Resultado: SUCESSO - source={source}, external_id={external_id}"))
    except Exception as e:
        log(_err(f"[TOOL AGENDAR] Resultado: EXCECAO - {e}"))
        logger.exception("Erro ao registrar agendamento para %s", phone)
    finally:
        _save_session_log(phone)


async def _update_summary_and_sheets(phone: str, name: str) -> None:
    """Gera resumo da conversa, salva no Redis e na planilha do Google."""
    _begin_session_log()
    log(f"[TOOL RESUMO] Executando generate_summary(phone={phone})")
    resumo = ""
    try:
        resumo = await generate_summary(phone)
        if resumo:
            await rds.update_lead(phone, resumo=resumo)
            log(_ok(f"[TOOL RESUMO] Resultado: SUCESSO - {len(resumo)} caracteres salvos no Redis"))
        else:
            log(_warn(f"[TOOL RESUMO] Resultado: vazio - historico insuficiente para gerar resumo"))
    except Exception as e:
        log(_err(f"[TOOL RESUMO] Resultado: EXCECAO - {e}"))
        logger.exception("Erro ao gerar resumo para %s: %s", phone, e)

    log(f"[TOOL SHEETS] Executando upsert_lead(phone={phone})")
    try:
        lead = await rds.get_lead(phone)
        sheets_service.upsert_lead(
            phone=phone,
            name=lead.get("name", name) if lead else name,
            resumo=resumo,
        )
        log(_ok(f"[TOOL SHEETS] Resultado: SUCESSO - lead atualizado na planilha"))
    except Exception as e:
        log(_err(f"[TOOL SHEETS] Resultado: EXCECAO - {e}"))
        logger.exception("Erro ao atualizar sheets para %s: %s", phone, e)
    finally:
        _save_session_log(phone)


async def start_consumer() -> None:
    """Inicia o consumer RabbitMQ."""
    await consume(_process_message)
