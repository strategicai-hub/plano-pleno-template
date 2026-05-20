"""
Fluxo 1: Webhook -> RabbitMQ
Recebe mensagens do WhatsApp (UAZAPI), filtra e publica na fila.
"""
import json
import logging

from fastapi import APIRouter, Request

from app import db
from app.config import settings
from app.services import redis_service as rds, uazapi
from app.services.rabbitmq import publish

logger = logging.getLogger(__name__)
router = APIRouter()



def _normalize_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "body", "content", "conversation"):
            text = _normalize_text(value.get(key, ""))
            if text:
                return text
    return ""


def _is_reset_confirmation(text: str) -> bool:
    normalized = " ".join((text or "").split()).casefold().rstrip(".!")
    return normalized == "conversa reiniciada"


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "sim"}
    return bool(value)


def _was_sent_by_api(msg: dict) -> bool:
    return any(
        _truthy(msg.get(key))
        for key in (
            "wasSentByApi",
            "wassentbyapi",
            "isSentByApi",
            "sentByApi",
            "fromApi",
        )
    )


@router.post(settings.WEBHOOK_PATH)
async def webhook(request: Request):
    payload = await request.json()

    msg = payload.get("message", {})

    # Filtra mensagens do proprio bot (IA) ou n8n
    track_source = msg.get("track_source", "")
    if track_source in ("n8n", "IA"):
        return {"status": "ignored", "reason": f"track_source={track_source}"}

    from_me = msg.get("fromMe", False)
    if from_me and _was_sent_by_api(msg):
        return {"status": "ignored", "reason": "sent by api"}

    # Quando fromMe=True (atendente humano enviou pelo WhatsApp Web/celular),
    # sender_pn e o numero DA EMPRESA e chatid e o do LEAD (destinatario).
    # Precisamos do numero do lead para bloquear o bot corretamente.
    if from_me:
        raw_sender = msg.get("chatid") or msg.get("sender_pn") or msg.get("sender", "")
    else:
        raw_sender = msg.get("sender_pn") or msg.get("chatid") or msg.get("sender", "")
    phone = raw_sender.split("@")[0] if raw_sender else ""
    chat_id = msg.get("chatid") or raw_sender
    push_name = msg.get("senderName", "")

    # Detecta tipo e conteudo da mensagem
    text = _normalize_text(msg.get("text", ""))
    msg_type_raw = msg.get("messageType", "")

    if text:
        msg_type = "Conversation"
        media_url = ""
        caption = ""
    elif msg_type_raw == "audioMessage" or "audioMessage" in msg:
        msg_type = "AudioMessage"
        media_url = msg.get("mediaUrl") or msg.get("url", "")
        caption = ""
    elif msg_type_raw == "imageMessage" or "imageMessage" in msg:
        msg_type = "ImageMessage"
        media_url = msg.get("mediaUrl") or msg.get("url", "")
        caption = msg.get("caption", "")
    else:
        msg_type = "Unknown"
        media_url = ""
        caption = ""

    # Descarta eventos sem telefone ou tipo nao suportado
    if not phone or msg_type == "Unknown":
        logger.warning(
            "Webhook ignorado (phone=%r, msg_type=%r). Payload bruto: %s",
            phone, msg_type, json.dumps(payload)[:2000],
        )
        return {"status": "ignored", "reason": "no phone or unsupported message"}

    if from_me and text and await rds.consume_outbound_echo(phone, text):
        logger.info("Eco outbound de %s ignorado", phone)
        return {"status": "ignored", "reason": "outbound echo"}

    if from_me and text and _is_reset_confirmation(text):
        logger.info("Confirmacao de reset outbound de %s ignorada", phone)
        return {"status": "ignored", "reason": "reset confirmation echo"}

    if phone in settings.blocked_sender_phones_set:
        logger.info("Mensagem de %s ignorada (BLOCKED_SENDER_PHONES)", phone)
        return {"status": "ignored", "reason": "phone blocked"}

    allowed = settings.allowed_phones_set
    if allowed and phone not in allowed:
        logger.info("Mensagem de %s ignorada (fora da whitelist ALLOWED_PHONES)", phone)
        return {"status": "ignored", "reason": "phone not in whitelist"}

    # /reset instantaneo — apaga TUDO do numero (Redis + SQLite) antes de
    # entrar na fila. Permite ao lead destravar o bot mesmo se estiver bloqueado.
    if (text or "").strip().lower() == "/reset":
        await rds.reset_lead_state(phone)
        await db.delete_lead(phone)
        try:
            await uazapi.send_text(phone, "Conversa reiniciada.")
        except Exception as e:
            logger.error("[%s] Falha ao confirmar reset: %s", phone, e)
        logger.info("[%s] Reset instantaneo via webhook", phone)
        return {"status": "reset"}

    queue_message = {
        "phone": phone,
        "push_name": push_name,
        "from_me": from_me,
        "msg_type": msg_type,
        "msg": text,
        "chat_id": chat_id,
        "media_url": media_url,
        "caption": caption,
        "raw_message": msg,
    }

    await publish(queue_message)
    logger.info("Mensagem de %s publicada na fila", phone)
    return {"status": "queued"}
