"""
Intake de leads para disparo de 1o contato.

Versao generica do template: recebe leads estruturados via HTTP do SAI Comercial
(POST /sai/leads) e os enfileira na lead_dispatch_queue do SQLite. O disparo da
1a mensagem fica com app/followups/lead_dispatch.py.
"""
import logging
import re

from app import db
from app.client_data import load_client_data

logger = logging.getLogger(__name__)


def only_digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def normalize_br_phone(raw: str) -> str:
    """Normaliza para digitos com DDI 55. Retorna "" se nao parecer BR valido."""
    digits = only_digits(raw)
    if not digits:
        return ""
    digits = digits.lstrip("0")  # zero de operadora/DDD (ex.: 021...)
    if digits.startswith("55") and len(digits) in (12, 13):
        return digits
    if len(digits) in (10, 11):  # DDD + assinante, sem DDI
        return "55" + digits
    return ""


def phone_variants(digits: str) -> set[str]:
    """Formas com e sem o 9o digito de um numero BR.

    JIDs do WhatsApp podem omitir o 9 em moveis registrados antes da
    migracao — o lead cadastrado como 5521 9XXXX-XXXX pode responder como
    5521XXXXXXXX. Matching, dedup e seeding de historico usam as duas formas.
    """
    variants = {digits}
    if digits.startswith("55"):
        ddd, subscriber = digits[2:4], digits[4:]
        if len(subscriber) == 9 and subscriber.startswith("9"):
            variants.add("55" + ddd + subscriber[1:])
        elif len(subscriber) == 8:
            variants.add("55" + ddd + "9" + subscriber)
    return variants


async def intake_http(
    leads: list[dict], tenant_slug: str
) -> tuple[int, int, int]:
    """Enfileira leads recebidos via HTTP do SAI Comercial (payload estruturado).

    Cada item: {"externalId", "name", "phone"}. O payload ja vem estruturado
    (nao parseia texto). Guarda o externalId para o callback de status.
    Retorna (enfileirados, dedupados, invalidos).
    """
    cfg = (load_client_data() or {}).get("lead_dispatch") or {}
    dedup_hours = int(cfg.get("dedup_hours", 72))
    source_phone = f"sai:{tenant_slug}"

    enqueued = skipped = invalid = 0
    for item in leads or []:
        phone = normalize_br_phone(str(item.get("phone") or ""))
        if not phone:
            invalid += 1
            continue
        nome = (str(item.get("name") or "")).strip()
        external_id = str(item.get("externalId") or "")
        _row_id, created = await db.enqueue_lead_dispatch(
            phone=phone,
            nome=nome,
            source_phone=source_phone,
            external_id=external_id,
            dedup_hours=dedup_hours,
            variants=phone_variants(phone),
        )
        if created:
            enqueued += 1
            logger.info(
                "[lead_intake] Lead HTTP %s (%s) enfileirado (externalId=%s, origem=%s)",
                phone, nome, external_id, source_phone,
            )
        else:
            skipped += 1
    return enqueued, skipped, invalid
