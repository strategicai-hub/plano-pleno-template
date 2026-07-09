"""Endpoints chamados pelo SAI Comercial (painel/inbox)."""
import logging
import re

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.services import redis_service, lead_intake
from app.client_data import load_client_data

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sai")


class BlockBody(BaseModel):
    phone: str
    blocked: bool


class LeadItem(BaseModel):
    externalId: str | None = None
    name: str | None = None
    phone: str


class LeadsBody(BaseModel):
    tenantSlug: str | None = None
    leads: list[LeadItem]


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


@router.post("/leads")
async def receive_leads(
    body: LeadsBody,
    x_ingest_secret: str | None = Header(default=None, alias="x-ingest-secret"),
):
    """Recebe uma lista de leads inserida no Painel IA do SAI e enfileira para
    disparo da 1a mensagem pela fila anti-bloqueio.

    O motor de disparo (lead_dispatch.run) consome a fila e, ao enviar, faz
    callback para o SAI marcando o lead como SENT. Autentica por x-ingest-secret.
    """
    if not settings.SAI_INGEST_SECRET or x_ingest_secret != settings.SAI_INGEST_SECRET:
        raise HTTPException(status_code=401, detail="invalid secret")

    cfg = (load_client_data() or {}).get("lead_dispatch") or {}
    if not cfg.get("http_intake_enabled", True):
        raise HTTPException(status_code=403, detail="http intake disabled")

    tenant_slug = (body.tenantSlug or settings.SAI_TENANT_SLUG or "sai").strip()
    leads = [
        {"externalId": li.externalId, "name": li.name, "phone": li.phone}
        for li in body.leads
    ]
    enqueued, skipped, invalid = await lead_intake.intake_http(leads, tenant_slug)
    logger.info(
        "sai_router: /leads recebeu %d (enfileirados=%d dedup=%d invalidos=%d) de %s",
        len(leads), enqueued, skipped, invalid, tenant_slug,
    )
    return {"ok": True, "enqueued": enqueued, "deduped": skipped, "invalid": invalid}
