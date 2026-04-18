"""
Fluxo 1: Webhook -> RabbitMQ
Recebe mensagens do WhatsApp (UAZAPI), filtra e publica na fila.
"""
import json
import logging

from fastapi import APIRouter, Request

from app.config import settings
from app.services.rabbitmq import publish

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(settings.WEBHOOK_PATH)
async def webhook(request: Request):
    payload = await request.json()

    msg = payload.get("message", {})

    # Filtra mensagens do proprio bot (IA) ou n8n
    track_source = msg.get("track_source", "")
    if track_source in ("n8n", "IA"):
        return {"status": "ignored", "reason": f"track_source={track_source}"}

    from_me = msg.get("fromMe", False)

    # Extrai telefone: tenta sender_pn, chatid, sender (mesma logica do projeto referencia)
    raw_sender = msg.get("sender_pn") or msg.get("chatid") or msg.get("sender", "")
    phone = raw_sender.split("@")[0] if raw_sender else ""
    chat_id = raw_sender
    push_name = msg.get("senderName", "")

    # Detecta tipo e conteudo da mensagem
    text = msg.get("text", "")
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
