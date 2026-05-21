"""Telemetria fire-and-forget de cada chamada IA para o SAI Comercial.

POST {SAI_BASE_URL}/api/ai/log/{SAI_TENANT_ID}
Header: x-ingest-secret = AssistantBot.ingestSecret (mesmo do contrato painel-ia-sync)

NUNCA propaga excecao, NUNCA bloqueia o bot. Se o SAI estiver fora, perdemos
telemetria daquele turno mas o bot segue respondendo normalmente.
"""
import asyncio
import logging
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)
_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=5.0)
    return _client


async def _post_log(payload: dict) -> None:
    if not settings.SAI_TENANT_ID or not settings.SAI_INGEST_SECRET:
        return  # Telemetria desligada por config; sem warning para nao poluir logs.
    try:
        client = _get_client()
        url = f"{settings.SAI_BASE_URL.rstrip('/')}/api/ai/log/{settings.SAI_TENANT_ID}"
        await client.post(
            url,
            headers={
                "x-ingest-secret": settings.SAI_INGEST_SECRET,
                "content-type": "application/json",
            },
            json=payload,
        )
    except Exception as e:
        logger.warning("sai_metrics post failed (ignored): %s", e)


def log_message_async(
    *,
    lead_phone: str,
    direction: str,        # "INBOUND" | "OUTBOUND"
    kind: str,             # "CHAT" | "TRANSCRIPTION" | "IMAGE_ANALYSIS" | "SUMMARY" | "REACTIVATION"
    model: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: Optional[int] = None,
) -> None:
    """Dispara o POST com asyncio.create_task — nao aguarda, nao bloqueia."""
    if input_tokens <= 0 and output_tokens <= 0:
        return  # Nada a logar.
    payload = {
        "leadPhone": lead_phone or "",
        "direction": direction,
        "kind": kind,
        "model": model,
        "inputTokens": int(input_tokens),
        "outputTokens": int(output_tokens),
        "latencyMs": latency_ms,
    }
    try:
        asyncio.create_task(_post_log(payload))
    except RuntimeError:
        # Sem loop ativo (chamada fora de contexto async) — descarta silenciosamente.
        pass
