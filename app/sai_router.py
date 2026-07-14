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


class DispatchContextBody(BaseModel):
    phone: str
    name: str | None = None
    route: str | None = None          # LOCACAO | VENDA_IMOVEL | VENDA_EMPREENDIMENTO
    sentMessage: str
    sentId: str | None = None
    empreendimentoFicha: str | None = None


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


_ROUTE_TOKENS = {"LOCACAO", "VENDA_IMOVEL", "VENDA_EMPREENDIMENTO"}


@router.post("/dispatch-context")
async def dispatch_context(
    body: DispatchContextBody,
    x_ingest_secret: str | None = Header(default=None, alias="x-ingest-secret"),
):
    """Ponte do motor antiban do SAI: avisa o bot que ELE (SAI) já disparou a
    1a mensagem de um lead corretor, informando a ROTA do Modelo.

    O disparo ativo do nicho corretor acontece 100% dentro do SAI (motor
    antiban) — o bot nunca saberia que a conversa começou ativa nem qual rota
    usar. Este endpoint semeia o histórico Redis com um [CONTEXTO DO SISTEMA:
    contato ATIVO ...] (espelhando o seeding do lead_dispatch legado), para que
    o prompt corretor_imoveis.j2 detecte o fluxo ATIVO, não se reapresente e faça
    a triagem da rota certa. Também registra o id da mensagem enviada como
    outbound, evitando que o eco fromMe do disparo seja lido como 'humano
    assumiu' e bloqueie o bot. Autentica por x-ingest-secret. Idempotente o
    suficiente (semear duas vezes só duplica um turno de contexto benigno).
    """
    if not settings.SAI_INGEST_SECRET or x_ingest_secret != settings.SAI_INGEST_SECRET:
        raise HTTPException(status_code=401, detail="invalid secret")

    phone = re.sub(r"\D+", "", body.phone or "")
    if not phone:
        raise HTTPException(status_code=400, detail="phone obrigatorio")
    if not (body.sentMessage or "").strip():
        raise HTTPException(status_code=400, detail="sentMessage obrigatorio")

    route = (body.route or "").strip().upper()
    if route not in _ROUTE_TOKENS:
        route = "VENDA_EMPREENDIMENTO"  # rota mais comum do disparo; fallback seguro
    nome = (body.name or "").strip()
    ficha = (body.empreendimentoFicha or "").strip()

    # Evita que o eco fromMe do disparo (relayado pela UAZAPI) vire set_block.
    if body.sentId:
        try:
            await redis_service.mark_outbound_id(str(body.sentId))
        except Exception:
            logger.warning("dispatch-context: mark_outbound_id falhou (seguindo)")

    contexto = (
        "[CONTEXTO DO SISTEMA: contato ATIVO iniciado por nós. "
        f"Rota: {route}. "
        f"Nome: {nome or '-'}. "
        + (f"Ficha do empreendimento: {ficha} " if ficha else "")
        + "Não se reapresente; quando o lead responder, faça a triagem da rota "
        f"{route} e, ao final, encaminhe ao corretor.]"
    )

    # Semeia nas duas variantes porque o JID da resposta pode vir com/sem o 9.
    variants = lead_intake.phone_variants(phone)
    for v in variants:
        await redis_service.append_chat_history(v, "user", contexto)
        await redis_service.append_chat_history(v, "model", body.sentMessage)

    if not await redis_service.get_lead(phone):
        await redis_service.create_lead(phone, nome)
    await redis_service.update_lead(
        phone, name=nome, status_conversa="Primeiro contato enviado"
    )

    logger.info("sai_router: /dispatch-context semeou ATIVO rota=%s para %s", route, phone)
    return {"ok": True, "phone": phone, "route": route}
