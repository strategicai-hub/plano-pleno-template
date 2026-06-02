"""Endpoints chamados pelo SAI Comercial (painel/inbox)."""
import logging
import re

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.services import redis_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sai")


class BlockBody(BaseModel):
    phone: str
    blocked: bool


@router.post("/block")
async def block_phone(
    body: BlockBody,
    x_ingest_secret: str | None = Header(default=None, alias="x-ingest-secret"),
):
    """Liga/desliga o bot para um telefone especifico.

    Chamado pelo SAI quando o operador clica 'Desligar IA' / 'Ligar IA' no
    header de uma conversa. blocked=True grava bloqueio permanente no Redis
    (sem TTL); blocked=False remove. Idempotente.
    """
    if not settings.SAI_INGEST_SECRET or x_ingest_secret != settings.SAI_INGEST_SECRET:
        raise HTTPException(status_code=401, detail="invalid secret")

    phone = re.sub(r"\D+", "", body.phone or "")
    if not phone:
        raise HTTPException(status_code=400, detail="phone obrigatorio")

    if body.blocked:
        await redis_service.set_permanent_block(phone, reason="manual")
        logger.info("sai_router: bot DESLIGADO manualmente para %s", phone)
    else:
        await redis_service.clear_block(phone)
        logger.info("sai_router: bot RELIGADO para %s", phone)

    return {"ok": True, "phone": phone, "blocked": body.blocked}
