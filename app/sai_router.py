"""Endpoints chamados pelo SAI Comercial (painel/inbox)."""
import logging
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from app import db
from app.config import settings
from app.services import redis_service, lead_intake, phone_utils, sai_sync
from app.client_data import load_client_data

logger = logging.getLogger(__name__)
router = APIRouter(prefix=f"{settings.WEBHOOK_PATH}/sai")


class BlockBody(BaseModel):
    phone: str
    blocked: bool
    # ISO 8601. Presente = pausa AUTOMATICA de "humano assumiu", que expira nesse
    # instante (o SAI usa o proximo 08:00 SP). Ausente = pausa do botao
    # "Desativar assistente", que nao expira. Bot antigo ignorava o campo e
    # gravava tudo como permanente — o que confundia os dois estados.
    resumeAt: str | None = None
    # True = a acao partiu do clique do operador no painel (Desativar/Ativar
    # assistente). Usado no religar: so o clique devolve o lead ao ciclo de
    # follow-up; o fim automatico do "humano assumiu" nao ressuscita cobranca.
    manual: bool = False


class LeadItem(BaseModel):
    externalId: str | None = None
    name: str | None = None
    phone: str
    # Origem declarada por quem entregou o lead (ex.: "META_LEAD_ADS" para o
    # formulario de Lead Ads da Meta). Opcional — lead sem origem segue o
    # roteiro generico de 1o contato.
    origin: str | None = None


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


class HistoryBody(BaseModel):
    phone: str
    role: str      # "attendant" (atendente humano) | "lead"
    content: str


class BindBody(BaseModel):
    tenantSlug: str | None = None
    ingestSecret: str | None = None


@router.post("/bind")
async def bind_tenant(
    body: BindBody,
    x_registration_token: str | None = Header(default=None, alias="x-registration-token"),
):
    """Vincula/desvincula este chatbot a um tenant do SAI.

    Chamado pelo SAI quando o super admin liga tenant -> chatbot no painel
    admin. Grava {tenantSlug, ingestSecret} no Redis; a partir daí o push de
    config e o polling passam a funcionar sem env var.
    """
    if (
        not settings.SAI_REGISTRATION_TOKEN
        or x_registration_token != settings.SAI_REGISTRATION_TOKEN
    ):
        raise HTTPException(status_code=401, detail="invalid token")

    if body.tenantSlug and body.ingestSecret:
        await sai_sync.save_binding(body.tenantSlug, body.ingestSecret)
        return {"ok": True, "bound": True, "tenantSlug": body.tenantSlug}

    await sai_sync.clear_binding()
    return {"ok": True, "bound": False}


@router.post("/config")
async def receive_config(
    request: Request,
    x_ingest_secret: str | None = Header(default=None, alias="x-ingest-secret"),
):
    """Recebe o snapshot completo do Painel IA WhatsApp (push a cada Save).

    O snapshot traz displayName, horarios e o catalogo de produtos — no nicho
    corretor de imoveis, cada empreendimento ativo entra no catalogo como um
    item com a ficha completa. Gravado em Redis, o prompt builder passa a usar
    esses dados em QUALQUER conversa (inclusive lead inbound), nao so no
    disparo.
    """
    cfg = await sai_sync._active_config_async()
    expected = cfg[1] if cfg else (settings.SAI_INGEST_SECRET or "")
    if not expected or x_ingest_secret != expected:
        raise HTTPException(status_code=401, detail="invalid secret")

    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload invalido")

    tenant_slug = payload.get("tenantSlug")
    if cfg and tenant_slug and tenant_slug != cfg[0]:
        raise HTTPException(status_code=400, detail="tenantSlug mismatch")

    # Bot que subiu so com SAI_INGEST_SECRET no env (sem SAI_TENANT_SLUG) nao
    # tem como saber em que chave gravar — aprende o slug pelo proprio payload
    # e persiste como binding, o que tambem liga o polling de fallback.
    if not cfg and tenant_slug:
        await sai_sync.save_binding(tenant_slug, expected)

    await sai_sync.save_snapshot(payload)
    logger.info("sai_router: snapshot recebido via push (tenantSlug=%s)", tenant_slug)
    return {"ok": True}


@router.post("/block")
async def block_phone(
    body: BlockBody,
    x_ingest_secret: str | None = Header(default=None, alias="x-ingest-secret"),
):
    """Liga/desliga o bot para um telefone especifico.

    Chamado pelo SAI em dois casos:
      * clique em 'Desativar assistente' / 'Ativar assistente' (manual=True);
      * pausa automatica de "humano assumiu" e seu fim (resumeAt preenchido).

    blocked=True sem `resumeAt` grava bloqueio PERMANENTE (so sai no 'Ativar
    assistente'); com `resumeAt`, grava com prazo — e a guarda do set_block
    impede que esse prazo rebaixe um bloqueio permanente que ja exista. Nos dois
    casos o lead sai do follow-up: nem cobranca automatica pode passar por cima
    de quem esta sendo atendido na mao ou teve o assistente desligado.

    O telefone chega do SAI canonicalizado COM o 9o digito, enquanto o bot
    indexa tudo pelo JID da UAZAPI (que costuma vir SEM o 9): por isso bloqueio
    e corte do follow-up sao aplicados nas DUAS variantes (ver phone_utils).
    Cortar no SQLite alem do Redis garante que um Redis perdido/limpo nao
    ressuscite o disparo proativo.
    """
    if not settings.SAI_INGEST_SECRET or x_ingest_secret != settings.SAI_INGEST_SECRET:
        raise HTTPException(status_code=401, detail="invalid secret")

    phone = re.sub(r"\D+", "", body.phone or "")
    if not phone:
        raise HTTPException(status_code=400, detail="phone obrigatorio")

    variants = phone_utils.block_variants(phone) or [phone]

    if body.blocked:
        ttl = _ttl_until(body.resumeAt)
        if ttl is None:
            await redis_service.set_permanent_block(phone, reason="manual")
            estado = "DESATIVADO (definitivo)"
        else:
            await redis_service.set_block(phone, ttl=ttl, reason="human")
            estado = f"pausado por {ttl}s (humano assumiu)"
        await db.mute_followups(variants, phone)
        logger.info(
            "sai_router: assistente %s para %s (variantes=%s) — follow-ups cortados",
            estado, phone, ",".join(variants),
        )
    else:
        await redis_service.clear_block(phone)
        # So o clique do operador devolve o lead ao ciclo de follow-up. O fim
        # automatico do "humano assumiu" nao pode ressuscitar cobranca em quem
        # a atendente ja pegou na mao.
        if body.manual:
            await db.unmute_followups(variants)
        logger.info(
            "sai_router: assistente REATIVADO para %s (variantes=%s, manual=%s)",
            phone, ",".join(variants), body.manual,
        )

    return {"ok": True, "phone": phone, "blocked": body.blocked, "variants": variants}


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
        {
            "externalId": li.externalId,
            "name": li.name,
            "phone": li.phone,
            "origin": li.origin,
        }
        for li in body.leads
    ]
    enqueued, skipped, invalid = await lead_intake.intake_http(leads, tenant_slug)
    logger.info(
        "sai_router: /leads recebeu %d (enfileirados=%d dedup=%d invalidos=%d) de %s",
        len(leads), enqueued, skipped, invalid, tenant_slug,
    )
    return {"ok": True, "enqueued": enqueued, "deduped": skipped, "invalid": invalid}


@router.post("/history")
async def push_history(
    body: HistoryBody,
    x_ingest_secret: str | None = Header(default=None, alias="x-ingest-secret"),
):
    """Acumula no historico Redis uma mensagem que o bot nao veria, sem gerar
    resposta da IA.

    Chamado fire-and-forget pelo SAI Comercial quando o gate de pausa suprime o
    relay do inbound ao bot (IA pausada — o lead escreveu durante o atendimento
    humano) ou quando o provider nao tem eco fromMe (API Oficial Meta — mensagem
    da atendente). role="attendant" grava como fala do bot (model); role="lead"
    grava como fala do lead (user). Assim, ao religar a IA, o bot retoma a
    conversa com o contexto completo em vez de se reapresentar do zero.
    """
    if not settings.SAI_INGEST_SECRET or x_ingest_secret != settings.SAI_INGEST_SECRET:
        raise HTTPException(status_code=401, detail="invalid secret")

    phone = re.sub(r"\D+", "", body.phone or "")
    content = (body.content or "").strip()
    if not phone or not content:
        raise HTTPException(status_code=400, detail="phone e content obrigatorios")

    role = "model" if body.role == "attendant" else "user"
    await redis_service.append_chat_history(phone, role, content)
    # Mesma regra do eco fromMe: falou a humana, o lead sai do follow-up para
    # sempre. Cobre o caso em que a mensagem dela nao gera eco fromMe e so
    # chega por aqui (API Oficial Meta / IA pausada no painel).
    if body.role == "attendant":
        await db.mute_followups(lead_intake.phone_variants(phone), phone)
    logger.info("sai_router: /history registrou %s (%d chars) para %s", body.role, len(content), phone)
    return {"ok": True, "phone": phone, "role": body.role}


def _ttl_until(resume_at: str | None) -> int | None:
    """Segundos ate `resume_at` (ISO 8601), ou None se ausente/invalido/passado.

    None = bloqueio sem prazo. Data invalida cai em None de proposito: na
    duvida, o bot fica MAIS calado, nunca menos.
    """
    if not resume_at:
        return None
    try:
        target = datetime.fromisoformat(str(resume_at).replace("Z", "+00:00"))
    except ValueError:
        logger.warning("sai_router: resumeAt invalido (%r) — tratando como sem prazo", resume_at)
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    seconds = int((target - datetime.now(timezone.utc)).total_seconds())
    return seconds if seconds > 0 else None


# META_FORM: lead que preencheu o formulario de Lead Ads da Meta. A abertura ja
# pediu permissao e ja fez a pergunta 1 do roteiro — o prompt trata essa rota
# separado das rotas do Painel IA.
_ROUTE_TOKENS = {"LOCACAO", "VENDA_IMOVEL", "VENDA_EMPREENDIMENTO", "META_FORM"}


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
    # A UAZAPI devolve o id em dois formatos ("owner:msgid" e "msgid" limpo) —
    # registramos os dois, senao o webhook nao reconhece o eco e bloqueia o bot.
    if body.sentId:
        try:
            sent_id = str(body.sentId)
            await redis_service.mark_outbound_id(sent_id)
            tail = sent_id.split(":")[-1]
            if tail and tail != sent_id:
                await redis_service.mark_outbound_id(tail)
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

    # Nome do lead — vai para o campo de CADASTRO, nunca para o de nome
    # confirmado: ele preencheu o formulario da origem (Lead Ads), mas ainda nao
    # disse nesta conversa como quer ser chamado. Ver app/services/nomes.py, e o
    # mesmo tratamento em followups/lead_dispatch.py.
    #
    # Grava nas DUAS variantes do numero, pelo mesmo motivo do historico acima:
    # o consumer resolve o nome com `get_lead(phone)` usando o telefone que veio
    # no JID da UAZAPI, que em muitos numeros chega SEM o 9o digito. Gravando so
    # a forma canonica, o lead responde, o consumer nao acha o cadastro, o
    # prompt recebe "NOME CONFIRMADO: (vazio)" e o bot PERGUNTA o nome que a
    # pessoa acabou de escrever no formulario.
    for v in variants:
        if not await redis_service.get_lead(v):
            await redis_service.create_lead(v)
        await redis_service.update_lead(
            v, name_cadastro=nome, status_conversa="Primeiro contato enviado"
        )
        await db.upsert_lead(v, nome_cadastro=nome)

    # Entra na regua de follow-up igual ao disparo local (lead_dispatch): quem
    # foi disparado pelo motor do SAI e nunca respondeu ficava fora da
    # reativacao, porque `_seed_inactive_leads` so pega quem tem
    # last_customer_message_at preenchido.
    ld_cfg = (load_client_data() or {}).get("lead_dispatch") or {}
    after_hours = int(ld_cfg.get("followup_after_hours", 24))
    if after_hours > 0:
        next_iso = (datetime.now(timezone.utc) + timedelta(hours=after_hours)).isoformat()
        await db.schedule_followup(phone, next_follow_up_iso=next_iso, stage=1)

    logger.info("sai_router: /dispatch-context semeou ATIVO rota=%s para %s", route, phone)
    return {"ok": True, "phone": phone, "route": route}
