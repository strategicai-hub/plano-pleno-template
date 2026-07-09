"""Sincronizacao com o Painel IA WhatsApp (SAI Comercial).

Fluxo:
  1. No deploy, o chatbot se auto-registra no catalogo do SAI
     (POST /api/chatbots/register) usando SAI_REGISTRATION_TOKEN.
  2. O super admin vincula tenant -> chatbot no painel. Nesse momento o SAI
     dispara POST /sai/bind no chatbot com {tenantSlug, ingestSecret} —
     gravado em Redis (chave `sai:binding`). Nao precisa setar env var.
  3. O SAI tambem dispara POST /sai/config a cada Save no painel
     (push fire-and-forget).
  4. Como fallback, este modulo poleia GET /api/ia/public/config/{slug}
     no startup e a cada 15 min — usando o binding do Redis.

Para retrocompat, se houver SAI_TENANT_SLUG/SAI_INGEST_SECRET no env, eles
servem como fallback quando nao houver binding no Redis.

Contrato em sai-comercial/docs/painel-ia-sync.md.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
import redis as redis_sync
from redis.asyncio import Redis

from app.config import settings
from app.services.redis_service import get_redis

log = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 15 * 60
HTTP_TIMEOUT_SECONDS = 10.0
LEGACY_BINDING_KEY = "sai:binding"  # compat: chave global antiga, usada quando varios chatbots compartilham Redis e o ultimo bind sobrescrevia os outros
IDLE_RETRY_SECONDS = 5 * 60  # quando nao ha binding, checa de novo em 5min


def _binding_key() -> str:
    """Chave isolada por chatbot — evita colisao quando varios bots compartilham Redis.

    Usa SAI_CHATBOT_SLUG; fallback em PROJECT_SLUG (definido em todo container);
    ultimo fallback em "default" so para nao quebrar em ambiente de teste.
    """
    slug = (settings.SAI_CHATBOT_SLUG or settings.PROJECT_SLUG or "default").strip() or "default"
    return f"sai:binding:{slug}"


def _snapshot_key(tenant_slug: str) -> str:
    return f"sai:config:{tenant_slug}"


def _register_configured() -> bool:
    return bool(
        settings.SAI_REGISTRATION_TOKEN
        and settings.SAI_CHATBOT_SLUG
        and settings.SAI_CHATBOT_PUBLIC_URL
    )


# --------------- binding (Redis) ---------------


async def save_binding(tenant_slug: str, ingest_secret: str) -> None:
    r: Redis = await get_redis()
    payload = json.dumps({"tenantSlug": tenant_slug, "ingestSecret": ingest_secret})
    await r.set(_binding_key(), payload)
    try:
        await r.delete(LEGACY_BINDING_KEY)
    except Exception:
        pass
    log.info("sai_sync: binding salvo (key=%s, tenant=%s)", _binding_key(), tenant_slug)


async def clear_binding() -> None:
    r: Redis = await get_redis()
    await r.delete(_binding_key())
    try:
        await r.delete(LEGACY_BINDING_KEY)
    except Exception:
        pass
    log.info("sai_sync: binding removido (key=%s)", _binding_key())


async def load_binding_async() -> tuple[str, str] | None:
    r: Redis = await get_redis()
    raw = await r.get(_binding_key())
    if not raw:
        raw = await r.get(LEGACY_BINDING_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        slug = data.get("tenantSlug")
        secret = data.get("ingestSecret")
        if slug and secret:
            return slug, secret
    except json.JSONDecodeError:
        pass
    return None


_sync_client: redis_sync.Redis | None = None


def _get_sync_client() -> redis_sync.Redis | None:
    global _sync_client
    if _sync_client is None:
        try:
            _sync_client = redis_sync.Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
        except Exception as exc:
            log.warning("sai_sync: nao conectou no redis (sync): %s", exc)
            return None
    return _sync_client


def load_binding_sync() -> tuple[str, str] | None:
    client = _get_sync_client()
    if client is None:
        return None
    try:
        raw = client.get(_binding_key())
        if not raw:
            raw = client.get(LEGACY_BINDING_KEY)
    except Exception as exc:
        log.warning("sai_sync: get binding sync falhou: %s", exc)
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        slug = data.get("tenantSlug")
        secret = data.get("ingestSecret")
        if slug and secret:
            return slug, secret
    except json.JSONDecodeError:
        pass
    return None


async def _active_config_async() -> tuple[str, str] | None:
    """(tenant_slug, ingest_secret) priorizando binding do Redis, fallback no env."""
    bound = await load_binding_async()
    if bound:
        return bound
    if settings.SAI_TENANT_SLUG and settings.SAI_INGEST_SECRET:
        return settings.SAI_TENANT_SLUG, settings.SAI_INGEST_SECRET
    return None


def _active_config_sync() -> tuple[str, str] | None:
    bound = load_binding_sync()
    if bound:
        return bound
    if settings.SAI_TENANT_SLUG and settings.SAI_INGEST_SECRET:
        return settings.SAI_TENANT_SLUG, settings.SAI_INGEST_SECRET
    return None


# --------------- auto-registro ---------------


async def register_with_sai() -> None:
    """Auto-registra este chatbot no catalogo do SAI.

    Fire-and-forget: o super admin vincula tenant -> chatbot via dropdown no
    painel admin. Se a env nao estiver configurada, vira no-op.
    """
    if not _register_configured():
        return
    url = f"{settings.SAI_BASE_URL.rstrip('/')}/api/chatbots/register"
    payload = {
        "slug": settings.SAI_CHATBOT_SLUG,
        "name": settings.SAI_CHATBOT_NAME or settings.SAI_CHATBOT_SLUG,
        "baseUrl": settings.SAI_CHATBOT_PUBLIC_URL.rstrip("/"),
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            res = await client.post(
                url,
                json=payload,
                headers={"x-registration-token": settings.SAI_REGISTRATION_TOKEN},
            )
            if res.status_code == 200:
                log.info("sai_sync: auto-registro OK (%s -> %s)", payload["slug"], payload["baseUrl"])
            else:
                log.warning("sai_sync: auto-registro %s -> %s: %s", url, res.status_code, res.text[:200])
    except Exception as exc:
        log.warning("sai_sync: auto-registro %s falhou: %s", url, exc)


# --------------- snapshot ---------------


async def save_snapshot(snapshot: dict[str, Any]) -> None:
    cfg = await _active_config_async()
    if not cfg:
        log.warning("sai_sync: save_snapshot chamado sem binding/env — descartado")
        return
    slug, _ = cfg
    r: Redis = await get_redis()
    await r.set(_snapshot_key(slug), json.dumps(snapshot, ensure_ascii=False))
    log.info("sai_sync: snapshot atualizado (slug=%s, updatedAt=%s)", slug, snapshot.get("updatedAt"))


async def load_snapshot() -> dict[str, Any] | None:
    cfg = await _active_config_async()
    if not cfg:
        return None
    slug, _ = cfg
    r: Redis = await get_redis()
    raw = await r.get(_snapshot_key(slug))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def load_snapshot_sync() -> dict[str, Any] | None:
    """Versao sincrona usada pelo prompt builder (que eh sync)."""
    cfg = _active_config_sync()
    if not cfg:
        return None
    slug, _ = cfg
    client = _get_sync_client()
    if client is None:
        return None
    try:
        raw = client.get(_snapshot_key(slug))
    except Exception as exc:
        log.warning("sai_sync: get snapshot sync falhou: %s", exc)
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


# --------------- polling ---------------


async def fetch_from_sai() -> dict[str, Any] | None:
    cfg = await _active_config_async()
    if not cfg:
        return None
    slug, secret = cfg
    url = f"{settings.SAI_BASE_URL.rstrip('/')}/api/ia/public/config/{slug}"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            res = await client.get(url, headers={"x-ingest-secret": secret})
            if res.status_code == 200:
                return res.json()
            log.warning("sai_sync: GET %s -> %s", url, res.status_code)
    except Exception as exc:
        log.warning("sai_sync: GET %s falhou: %s", url, exc)
    return None


async def sync_now() -> None:
    snap = await fetch_from_sai()
    if snap:
        await save_snapshot(snap)


# --------------- push de agendamentos ---------------


async def push_appointment(payload: dict[str, Any]) -> None:
    """Reporta um agendamento ao SAI Comercial (fire-and-forget).

    `payload` deve seguir o contrato em sai-comercial/docs/ingest-api.md:
      scheduledAt (ISO), externalId?, durationMin?, contactName?, contactPhone?,
      status?, modality?, notes?
    """
    cfg = await _active_config_async()
    if not cfg:
        log.warning(
            "sai_sync: push_appointment descartado — sem binding no Redis e sem "
            "SAI_TENANT_SLUG/SAI_INGEST_SECRET no env. Agendamento NAO sera "
            "reportado ao SAI Comercial. payload=%s",
            {k: payload.get(k) for k in ("scheduledAt", "contactPhone", "externalId")},
        )
        return
    slug, secret = cfg
    url = f"{settings.SAI_BASE_URL.rstrip('/')}/api/assistant/ingest/appointments/by-slug/{slug}"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            res = await client.post(
                url,
                json=payload,
                headers={"x-ingest-secret": secret, "Content-Type": "application/json"},
            )
            if res.status_code in (200, 201):
                log.info(
                    "sai_sync: agendamento reportado (slug=%s, status=%s, externalId=%s)",
                    slug, res.status_code, payload.get("externalId"),
                )
            else:
                log.warning("sai_sync: POST %s -> %s: %s", url, res.status_code, res.text[:200])
    except Exception as exc:
        log.warning("sai_sync: POST %s falhou: %s", url, exc)


async def cancel_appointment(
    *,
    contact_phone: str,
    external_id: str | None = None,
    reason: str | None = None,
) -> None:
    """Cancela o agendamento mais recente do contato no SAI Comercial (fire-and-forget)."""
    cfg = await _active_config_async()
    if not cfg:
        log.warning("sai_sync: cancel_appointment descartado — sem binding (phone=%s)", contact_phone)
        return
    slug, secret = cfg
    payload: dict[str, Any] = {"contactPhone": contact_phone.replace("@s.whatsapp.net", "")}
    if external_id:
        payload["externalId"] = external_id
    if reason:
        payload["reason"] = reason
    url = f"{settings.SAI_BASE_URL.rstrip('/')}/api/assistant/ingest/appointments/by-slug/{slug}/cancel"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            res = await client.post(
                url,
                json=payload,
                headers={"x-ingest-secret": secret, "Content-Type": "application/json"},
            )
            if res.status_code == 200:
                log.info(
                    "sai_sync: cancelamento reportado (slug=%s, phone=%s, body=%s)",
                    slug, payload["contactPhone"], res.text[:200],
                )
            else:
                log.warning("sai_sync: POST %s -> %s: %s", url, res.status_code, res.text[:200])
    except Exception as exc:
        log.warning("sai_sync: POST %s falhou: %s", url, exc)


async def report_lead_sent(
    *,
    external_id: str | None,
    phone: str,
    status: str = "SENT",
    reason: str | None = None,
) -> None:
    """Reporta ao SAI que a 1a mensagem de um lead foi enviada.

    Chamado logo apos mark_dispatch_sent. Fire-and-forget: nunca lanca. O SAI
    localiza o lead por externalId (preferido) ou phoneE164 e marca SENT/FAILED,
    migrando-o para a tabela 'Ja contatados' no painel.
    """
    cfg = await _active_config_async()
    if not cfg:
        log.warning("sai_sync: report_lead_sent descartado — sem binding (phone=%s)", phone)
        return
    slug, secret = cfg
    payload: dict[str, Any] = {"status": status, "phoneE164": phone}
    if external_id:
        payload["externalId"] = external_id
    if status == "SENT":
        payload["sentAt"] = datetime.now(timezone.utc).isoformat()
    elif reason:
        payload["reason"] = reason
    url = f"{settings.SAI_BASE_URL.rstrip('/')}/api/assistant/ingest/leads/by-slug/{slug}/sent"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            res = await client.post(
                url,
                json=payload,
                headers={"x-ingest-secret": secret, "Content-Type": "application/json"},
            )
            if res.status_code == 200:
                log.info(
                    "sai_sync: lead reportado (slug=%s, externalId=%s, status=%s)",
                    slug, external_id, status,
                )
            else:
                log.warning("sai_sync: POST %s -> %s: %s", url, res.status_code, res.text[:200])
    except Exception as exc:
        log.warning("sai_sync: POST %s falhou: %s", url, exc)


async def start_polling() -> None:
    """Loop infinito, executado em background no lifespan do FastAPI.

    Sempre roda o auto-registro uma unica vez antes do primeiro check de
    binding. Mesmo sem binding, mantem o loop vivo checando a cada
    IDLE_RETRY_SECONDS — assim que o super admin vincular, o push do SAI
    salva o binding e o proximo tick comeca a sincronizar.
    """
    await register_with_sai()
    log.info("sai_sync: loop iniciado")
    first_run = True
    while True:
        try:
            cfg = await _active_config_async()
            if not cfg:
                if first_run:
                    log.info(
                        "sai_sync: sem binding — aguardando vinculo no SAI (checa a cada %ss)",
                        IDLE_RETRY_SECONDS,
                    )
                    first_run = False
                await asyncio.sleep(IDLE_RETRY_SECONDS)
                continue
            if first_run:
                log.info("sai_sync: polling ativo (slug=%s, intervalo=%ss)", cfg[0], POLL_INTERVAL_SECONDS)
                first_run = False
            await sync_now()
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("sai_sync: erro no loop: %s", exc)
            await asyncio.sleep(60)
