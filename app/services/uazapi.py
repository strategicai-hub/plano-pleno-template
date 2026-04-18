import json as _json
import logging

import httpx

from app.config import settings

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


async def send_text(number: str, text: str, delay: int = 4000) -> dict:
    url = f"{settings.UAZAPI_BASE_URL}/send/text"
    payload = {"number": number, "text": text, "delay": delay}
    client = _get_client()
    resp = await client.post(url, content=_json_body(payload), headers=_headers())
    resp.raise_for_status()
    logger.info("Texto enviado para %s", number)
    return resp.json()


async def _send_media(number: str, media_type: str, file_url: str, delay: int = 4000) -> dict:
    url = f"{settings.UAZAPI_BASE_URL}/send/media"
    payload = {"number": number, "type": media_type, "file": file_url, "delay": delay}
    client = _get_client()
    resp = await client.post(url, content=_json_body(payload), headers=_headers())
    resp.raise_for_status()
    logger.info("%s enviado para %s", media_type, number)
    return resp.json()


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
