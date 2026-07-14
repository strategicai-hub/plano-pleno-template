"""Endpoints de SIMULACAO do bot (usados pelo SAI Comercial).

Reproduzem o MESMO cerebro do bot real (gemini.chat + prompt do painel +
transcricao/visao), mas por HTTP sincrono, sem UAZAPI, sem fila e sem efeitos
colaterais (agendamento/alertas/sheets). Servem para o cliente testar o bot
antes de entrar em producao, fazendo o papel de um lead.

Diferencas em relacao ao fluxo de producao:
- Debounce de SIM_DEBOUNCE_SECONDS (15s) em vez de DEBOUNCE_SECONDS (30s).
- Sessao isolada por session_id (phone virtual "sim_<session_id>"), para nao
  poluir o historico de leads reais no Redis.
- Autenticacao por header x-sim-secret == SAI_INGEST_SECRET (server-to-server;
  o navegador do cliente nunca ve o segredo — fala com o proxy do SAI).
"""
import asyncio
import base64
import logging
import re

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.consumer import _parse_ai_response
from app.services import gemini, redis_service as rds

# Prefixo na RAIZ (espelha sai_router "/sai"), NAO sob WEBHOOK_PATH — o SAI
# anexa "/sim/*" direto na baseUrl do chatbot, igual faz com "/sai/*".
logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sim")


def _sim_phone(session_id: str) -> str:
    """Deriva um 'phone' virtual isolado a partir do session_id do navegador.

    Sanitiza para caracteres seguros no namespacing do Redis (<phone>--<slug>).
    """
    clean = re.sub(r"[^A-Za-z0-9_-]", "", session_id or "")[:64]
    if not clean:
        raise HTTPException(status_code=400, detail="session_id obrigatorio")
    return f"sim_{clean}"


def _check_secret(x_sim_secret: str | None) -> None:
    if not settings.SAI_INGEST_SECRET or x_sim_secret != settings.SAI_INGEST_SECRET:
        raise HTTPException(status_code=401, detail="invalid secret")


class SimMessageBody(BaseModel):
    session_id: str
    type: str = "text"           # "text" | "audio" | "image"
    text: str = ""
    media_base64: str = ""
    mime: str = ""
    caption: str = ""


class SimResetBody(BaseModel):
    session_id: str


async def _normalize(body: SimMessageBody, phone: str) -> str:
    """Converte o item recebido (texto/audio/imagem) no texto que entra no chat.

    Espelha o bloco E) de consumer._process_message, porem a partir de bytes de
    upload (base64) em vez de baixar a URL da UAZAPI.
    """
    mtype = (body.type or "text").lower()
    if mtype == "audio":
        if not body.media_base64:
            return "[Audio recebido - nao foi possivel transcrever]"
        try:
            audio_bytes = base64.b64decode(body.media_base64)
            transcription = await gemini.transcribe_audio(
                audio_bytes, phone, mime_type=body.mime or "audio/ogg"
            )
            return f"[Audio transcrito]: {transcription}"
        except Exception:
            logger.exception("sim: erro ao transcrever audio")
            return "[Audio recebido - erro na transcricao]"
    if mtype == "image":
        if not body.media_base64:
            return "[Imagem recebida - nao foi possivel analisar]"
        try:
            image_bytes = base64.b64decode(body.media_base64)
            description = await gemini.analyze_image(
                image_bytes, phone, mime_type=body.mime or "image/jpeg"
            )
            out = f"[Imagem recebida]: {description}"
            if body.caption:
                out += f"\nLegenda: {body.caption}"
            return out
        except Exception:
            logger.exception("sim: erro ao analisar imagem")
            return "[Imagem recebida - erro na analise]"
    return (body.text or "").strip()


@router.post("/message")
async def sim_message(
    body: SimMessageBody,
    x_sim_secret: str | None = Header(default=None, alias="x-sim-secret"),
):
    """Recebe uma mensagem do 'lead' simulado e (apos o debounce) devolve a
    resposta do bot. Agrega rajadas: a primeira mensagem segura ~15s; as
    seguintes apenas empilham no buffer e voltam com buffered=True."""
    _check_secret(x_sim_secret)
    phone = _sim_phone(body.session_id)

    buffer_text = await _normalize(body, phone)
    if not buffer_text:
        raise HTTPException(status_code=400, detail="mensagem vazia")

    count = await rds.push_buffer(phone, buffer_text)
    if count > 1:
        # Ja ha uma task segurando o debounce desta sessao; so empilhamos.
        return {"buffered": True}

    await asyncio.sleep(settings.SIM_DEBOUNCE_SECONDS)
    messages = await rds.pop_buffer(phone)
    unified = "\n".join(messages)

    ai_response, tokens = await gemini.chat(phone, unified)
    parts, finalizado, transferir, *_ = _parse_ai_response(ai_response)
    return {
        "buffered": False,
        "parts": parts,
        "finalizado": finalizado,
        "transferir": transferir,
        "tokens": {"input": tokens[0], "output": tokens[1], "total": tokens[2]},
    }


@router.post("/reset")
async def sim_reset(
    body: SimResetBody,
    x_sim_secret: str | None = Header(default=None, alias="x-sim-secret"),
):
    """Limpa todo o estado da sessao de simulacao (botao 'Nova conversa')."""
    _check_secret(x_sim_secret)
    phone = _sim_phone(body.session_id)
    await rds.reset_lead_state(phone)
    return {"ok": True}


@router.get("/history")
async def sim_history(
    session_id: str,
    x_sim_secret: str | None = Header(default=None, alias="x-sim-secret"),
):
    """Historico parseado da sessao — usado para repopular a tela ao reabrir."""
    _check_secret(x_sim_secret)
    phone = _sim_phone(session_id)
    history = await rds.get_chat_history(phone)
    out = []
    for entry in history:
        role = entry.get("role", "")
        text = (entry.get("parts", [{}]) or [{}])[0].get("text", "")
        if not text:
            continue
        if role == "model":
            parts, *_ = _parse_ai_response(text)
        else:
            parts = [{"type": "text", "content": text}]
        out.append({"role": role, "parts": parts})
    return {"messages": out}
