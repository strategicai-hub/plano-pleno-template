"""
Fluxo 1: Webhook -> RabbitMQ
Recebe mensagens do WhatsApp (UAZAPI), filtra e publica na fila.
"""
import asyncio
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


# customId que o SAI Comercial carimba em cada envio. "human" = atendente
# respondeu pelo painel (o unico envio por API que representa gente de verdade).
_CUSTOM_ID_HUMAN = "human"


def _is_api_send_without_human(msg: dict) -> bool:
    """True quando o eco fromMe veio de um envio por API que NAO e a atendente.

    Sem isso, qualquer mensagem enviada direto na UAZAPI (follow-up manual,
    script de operacao, outro bot) chegava aqui como fromMe "anonimo" e o
    consumer travava o assistente ate as 08:00 do dia seguinte — enquanto o
    painel do SAI seguia mostrando a IA ligada, porque la esse mesmo envio e
    classificado como `sentFromBot` e nao pausa nada. O lead ficava sem
    resposta sem nenhum sinal visivel de que o bot estava mudo.

    Atendente digitando no celular chega com wasSentByApi=False e continua
    travando; atendente pelo painel do SAI chega com customId="human" e
    tambem continua travando.
    """
    custom_id = str(msg.get("customId") or msg.get("custom_id") or "").strip().lower()
    if custom_id == _CUSTOM_ID_HUMAN:
        return False
    was_sent_by_api = bool(
        msg.get("wasSentByApi")
        or msg.get("wassentbyapi")
        or msg.get("was_sent_by_api")
        or msg.get("fromApi")
        or msg.get("from_api")
    )
    return was_sent_by_api



@router.post(settings.WEBHOOK_PATH)
async def webhook(request: Request):
    payload = await request.json()

    msg = payload.get("message", {})

    # Filtra mensagens do proprio bot (IA) ou n8n
    track_source = msg.get("track_source", "")
    if track_source in ("n8n", "IA"):
        return {"status": "ignored", "reason": f"track_source={track_source}"}

    from_me = msg.get("fromMe", False)

    # Eco do proprio bot: mensagens enviadas pelo bot voltam (reenviadas pelo
    # SAI Comercial) com fromMe=True. Identificamos pelo id exato registrado no
    # envio. `wasSentByApi` sozinho nao serve de filtro — a atendente humana
    # respondendo pelo painel tambem chega assim; por isso o descarte olha o
    # customId (ver _is_api_send_without_human). O filtro de track_source
    # ("IA"/"n8n") acima ja cobre os ecos do bot; este check por id e a rede de
    # seguranca caso track_source se perca.
    if from_me:
        # A UAZAPI entrega o mesmo id em dois formatos: `id` = "owner:msgid"
        # (com prefixo do numero da instancia) e `messageid` = "msgid" limpo.
        # Quem registrou o outbound (bot ou SAI, via /sai/dispatch-context) pode
        # ter gravado qualquer um dos dois — checar so `id` deixava o eco passar.
        raw_ids = [msg.get("id"), msg.get("messageid"), msg.get("messageId")]
        candidates: list[str] = []
        for raw in raw_ids:
            if not raw:
                continue
            s = str(raw)
            if s not in candidates:
                candidates.append(s)
            tail = s.split(":")[-1]
            if tail and tail not in candidates:
                candidates.append(tail)
        for cand in candidates:
            if await rds.is_outbound_id(cand):
                return {"status": "ignored", "reason": "own outbound echo (id)"}
        if _is_api_send_without_human(msg):
            logger.info("Eco fromMe por API sem customId=human — ignorado (nao trava o bot)")
            return {"status": "ignored", "reason": "api send, not human takeover"}

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
    # A UAZAPI manda messageType em PascalCase ("AudioMessage", "ImageMessage"),
    # nao em camelCase minusculo. Comparar sem normalizar caixa faz TODO audio
    # e imagem cair em "Unknown" e ser descartado em silencio (nem resposta,
    # nem fila, nem log de erro visivel) — foi o caso de uma nota de voz que a
    # dra-milena-catani ignorou em 03/09/2026 (mesmo bug deste template).
    msg_type_raw = (msg.get("messageType") or "").lower()
    content = msg.get("content") if isinstance(msg.get("content"), dict) else {}

    if text:
        msg_type = "Conversation"
        media_url = ""
        caption = ""
    elif msg_type_raw == "audiomessage" or "audioMessage" in msg:
        msg_type = "AudioMessage"
        # Audio "normal" costuma vir com mediaUrl/url no topo; nota de voz
        # (PTT) so tem o link criptografado em content.URL. Sem esse fallback
        # o tipo era reconhecido mas o download falhava sem media_url.
        media_url = msg.get("mediaUrl") or msg.get("url") or content.get("URL", "")
        caption = ""
    elif msg_type_raw == "imagemessage" or "imageMessage" in msg:
        msg_type = "ImageMessage"
        media_url = msg.get("mediaUrl") or msg.get("url") or content.get("URL", "")
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

    # id da mensagem na UAZAPI — usado para idempotência no consumer (dedup de
    # reentrega). Campos variam conforme versão/payload; pega o primeiro válido.
    message_id = (
        msg.get("id")
        or msg.get("messageid")
        or msg.get("messageId")
        or (msg.get("key") or {}).get("id")
        or ""
    )

    queue_message = {
        "phone": phone,
        "push_name": push_name,
        "from_me": from_me,
        "msg_type": msg_type,
        "msg": text,
        "chat_id": chat_id,
        "media_url": media_url,
        "caption": caption,
        "message_id": message_id,
        "raw_message": msg,
    }

    # Tique azul: marca a mensagem do lead como lida assim que chega (so mensagens
    # recebidas, nunca fromMe nem grupos). Fire-and-forget para nao atrasar a fila.
    if not from_me and message_id and "@g.us" not in chat_id:
        asyncio.create_task(uazapi.mark_read(message_id))

    await publish(queue_message)
    logger.info("Mensagem de %s publicada na fila", phone)
    return {"status": "queued"}
