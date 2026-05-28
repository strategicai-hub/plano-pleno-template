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
    client = _get_client()
    resp = await client.post(url, content=_json_body(payload), headers=_headers())
    resp.raise_for_status()
    data = resp.json()
    await _remember_outbound(data)
    logger.info("Texto enviado para %s", number)
    return data


async def _send_media(number: str, media_type: str, file_url: str, delay: int = 4000) -> dict:
    url = f"{settings.UAZAPI_BASE_URL}/send/media"
    payload = {
        "number": number,
        "type": media_type,
        "file": file_url,
        "delay": delay,
        "track_source": TRACK_SOURCE,
    }
    client = _get_client()
    resp = await client.post(url, content=_json_body(payload), headers=_headers())
    resp.raise_for_status()
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


async def download_media(media_url: str) -> bytes:
    client = _get_client()
    resp = await client.get(media_url, headers=_headers())
    resp.raise_for_status()
    return resp.content
