"""
Intake de leads para disparo de 1o contato.

Versao generica do template: recebe leads estruturados via HTTP do SAI Comercial
(POST /sai/leads) e os enfileira na lead_dispatch_queue do SQLite. O disparo da
1a mensagem fica com app/followups/lead_dispatch.py.
"""
import logging

from app import db
from app.client_data import load_client_data

# Normalizacao vive em phone_utils (modulo sem dependencias) para que
# redis_service tambem a use sem ciclo de import. Re-exportado aqui por
# retrocompat — varios modulos importam `lead_intake.phone_variants`.
from app.services.phone_utils import (  # noqa: F401
    only_digits,
    normalize_br_phone,
    phone_variants,
)

logger = logging.getLogger(__name__)


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
