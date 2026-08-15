import asyncio
import json as _json
import logging

import httpx

from app.config import settings
from app.services import redis_service as rds

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=30)
    return _client


def _headers() -> dict:
    return {
        "Content-Type": "application/json; charset=utf-8",
        "token": settings.UAZAPI_TOKEN,
    }


def _json_body(payload: dict) -> bytes:
    """Serializa payload preservando UTF-8 (ç, á, é etc.) sem escape unicode."""
    return _json.dumps(payload, ensure_ascii=False).encode("utf-8")

# Backoff dos reenvios. Motivo: em 14/08/2026 a instancia caiu ("logged out from
# another device") entre o balao 1 e o 2 de uma resposta de 4 partes — a UAZAPI
# passou a devolver 503 e o lead ficou so com o cumprimento, sem a pergunta. Sem
# retry, qualquer indisponibilidade de segundos vira lead abandonado.
_RETRY_DELAYS = (2, 5, 12)


def _is_transient(exc: Exception) -> bool:
    """So repete o que tem chance real de dar certo na proxima tentativa.

    5xx = servidor UAZAPI/WhatsApp instavel ou instancia reconectando.
    Timeout/erro de conexao = rede. 4xx (numero invalido, token errado) nunca
    melhora com repeticao — falha na hora para o alerta subir rapido.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


async def _post_with_retry(url: str, payload: dict, what: str, number: str) -> "httpx.Response":
    """POST na UAZAPI com reenvio automatico em falha transitoria."""
    client = _get_client()
    last: Exception | None = None
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            resp = await client.post(url, content=_json_body(payload), headers=_headers())
            resp.raise_for_status()
            if attempt:
                logger.info("%s para %s enviado na tentativa %d", what, number, attempt + 1)
            return resp
        except Exception as exc:  # noqa: BLE001 - reclassificado logo abaixo
            last = exc
            if attempt >= len(_RETRY_DELAYS) or not _is_transient(exc):
                break
            delay = _RETRY_DELAYS[attempt]
            logger.warning(
                "Falha ao enviar %s para %s (tentativa %d/%d): %s — retentando em %ds",
                what, number, attempt + 1, len(_RETRY_DELAYS) + 1, exc, delay,
            )
            await asyncio.sleep(delay)
    raise last  # type: ignore[misc]


# Toda mensagem enviada pelo bot vai marcada com este track_source. O webhook
# filtra `track_source in ("n8n", "IA")` (fromMe), entao os ecos do proprio bot
# (reenviados pelo SAI Comercial) sao descartados — sem isso, o bot se
# autobloquearia a cada envio quando paramos de descartar `wasSentByApi`.
TRACK_SOURCE = "IA"


async def _remember_outbound(resp_json: dict) -> None:
    """Marca o(s) id(s) da mensagem recem-enviada como eco do proprio bot."""
    if not isinstance(resp_json, dict):
        return
    candidates = [resp_json, resp_json.get("message")]
    for obj in candidates:
        if not isinstance(obj, dict):
            continue
        for k in ("messageid", "id"):
            v = obj.get(k)
            if isinstance(v, str) and v:
                await rds.mark_outbound_id(v)


async def send_text(number: str, text: str, delay: int = 4000) -> dict:
    url = f"{settings.UAZAPI_BASE_URL}/send/text"
    payload = {"number": number, "text": text, "delay": delay, "track_source": TRACK_SOURCE}
    await rds.mark_outbound_echo(number, text)
    resp = await _post_with_retry(url, payload, "texto", number)
    data = resp.json()
    await _remember_outbound(data)
    logger.info("Texto enviado para %s", number)
    return data


async def send_presence(number: str, presence: str = "composing") -> None:
    """Emite presenca (digitando.../gravando.../online) para o numero.

    Endpoint correto deste servidor UAZAPI: POST /message/presence com
    {number, presence}. Valores validos confirmados: "composing" (digitando),
    "recording" (gravando audio), "available", "paused". NAO usar /chat/presence
    nem /send/presence — esses paths retornam 405 (nao existem) neste servidor.

    Presenca e efemera no WhatsApp — chamar antes de cada balao para o usuario
    ver "digitando..." enquanto a IA gera/responde.
    """
    url = f"{settings.UAZAPI_BASE_URL}/message/presence"
    payload = {"number": number, "presence": presence}
    try:
        client = _get_client()
        resp = await client.post(url, content=_json_body(payload), headers=_headers())
        resp.raise_for_status()
    except Exception as e:
        # Presenca e nice-to-have — nunca derruba o fluxo principal.
        logger.warning("Falha ao enviar presence %s para %s: %s", presence, number, e)


async def mark_read(message_id: str) -> None:
    """Marca a(s) mensagem(ns) do lead como lida(s) -> tique azul no WhatsApp dele.

    Endpoint correto deste servidor UAZAPI: POST /message/markread com {id: [...]}.
    Chamado no webhook assim que a mensagem chega, para o lead ver os dois tiques
    azuis instantaneamente (como num atendimento humano).

    Fire-and-forget: qualquer falha so loga, nunca derruba o fluxo de recebimento.
    """
    if not message_id:
        return
    url = f"{settings.UAZAPI_BASE_URL}/message/markread"
    payload = {"id": [message_id]}
    try:
        client = _get_client()
        resp = await client.post(url, content=_json_body(payload), headers=_headers())
        resp.raise_for_status()
        logger.info("Mensagem %s marcada como lida (tique azul)", message_id)
    except Exception as exc:
        logger.warning("Falha ao marcar %s como lida: %s", message_id, exc)


async def _send_media(number: str, media_type: str, file_url: str, delay: int = 4000) -> dict:
    url = f"{settings.UAZAPI_BASE_URL}/send/media"
    payload = {
        "number": number,
        "type": media_type,
        "file": file_url,
        "delay": delay,
        "track_source": TRACK_SOURCE,
    }
    resp = await _post_with_retry(url, payload, media_type, number)
    data = resp.json()
    await _remember_outbound(data)
    logger.info("%s enviado para %s", media_type, number)
    return data


async def send_image(number: str, image_url: str, caption: str = "") -> dict:
    return await _send_media(number, "image", image_url)


async def send_document(number: str, document_url: str, filename: str = "arquivo.pdf") -> dict:
    return await _send_media(number, "document", document_url)


async def send_video(number: str, video_url: str, caption: str = "") -> dict:
    return await _send_media(number, "video", video_url)


async def get_instance_status() -> dict:
    """Estado da instancia WhatsApp (connected / disconnected / connecting).

    Usado pelo vigia de conexao. Quando o aparelho e deslogado ("logged out from
    another device"), /send/text passa a responder 503 e o bot fica mudo sem que
    ninguem perceba — este endpoint e a unica forma de detectar isso sozinho.
    """
    url = f"{settings.UAZAPI_BASE_URL}/instance/status"
    client = _get_client()
    resp = await client.get(url, headers=_headers())
    resp.raise_for_status()
    data = resp.json() or {}
    return data.get("instance") or data

async def download_media(media_url: str) -> bytes:
    client = _get_client()
    resp = await client.get(media_url, headers=_headers())
    resp.raise_for_status()
    return resp.content
