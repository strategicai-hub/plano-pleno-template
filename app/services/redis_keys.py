"""
Fonte unica de verdade para nomes de chaves do Redis deste projeto.

Schema:
    <phone>--<slug>:<type>    -> dados por lead (buffer, lead, history, block, alert)
    <slug>:logs               -> lista global de logs de execucao
    <slug>:*                  -> qualquer outra chave global do projeto
"""
from app.config import settings


def _phone_ns(phone: str) -> str:
    return f"{phone}--{settings.PROJECT_SLUG}"


def buffer_key(phone: str) -> str:
    return f"{_phone_ns(phone)}:buffer"


def lead_key(phone: str) -> str:
    return f"{_phone_ns(phone)}:lead"


def history_key(phone: str) -> str:
    return f"{_phone_ns(phone)}:history"


def block_key(phone: str) -> str:
    return f"{_phone_ns(phone)}:block"


def alert_key(phone: str) -> str:
    return f"{_phone_ns(phone)}:alert"


def outbound_echo_key(phone: str, digest: str) -> str:
    return f"{_phone_ns(phone)}:outbound:{digest}"
def outbound_id_key(msg_id: str) -> str:
    # Global (id de mensagem ja e unico) — marca ecos do proprio bot por id exato.
    return f"{settings.PROJECT_SLUG}:outbound-id:{msg_id}"


def followup_active_key(phone: str) -> str:
    return f"{_phone_ns(phone)}:followup:active"


def followup_lock_key(phone: str) -> str:
    """Trava distribuida para impedir dois envios concorrentes do mesmo FUP
    (race entre instancias do scheduler durante rolling update / overlap)."""
    return f"{_phone_ns(phone)}:followup:lock"


def processed_key(message_id: str) -> str:
    """Chave de idempotência por message_id da UAZAPI (dedup de reentrega)."""
    return f"{settings.PROJECT_SLUG}:processed:{message_id}"


def session_log_key() -> str:
    return f"{settings.PROJECT_SLUG}:logs"


# --- patterns + helpers para as rotas de leitura ---

def lead_scan_pattern() -> str:
    return f"*--{settings.PROJECT_SLUG}:lead"


def history_scan_pattern() -> str:
    return f"*--{settings.PROJECT_SLUG}:history"


def phone_from_lead_key(key: str) -> str:
    suffix = f"--{settings.PROJECT_SLUG}:lead"
    return key[: -len(suffix)] if key.endswith(suffix) else key


def phone_from_history_key(key: str) -> str:
    suffix = f"--{settings.PROJECT_SLUG}:history"
    return key[: -len(suffix)] if key.endswith(suffix) else key


def buffer_scan_pattern() -> str:
    return f"*--{settings.PROJECT_SLUG}:buffer"


def phone_from_buffer_key(key: str) -> str:
    suffix = f"--{settings.PROJECT_SLUG}:buffer"
    return key[: -len(suffix)] if key.endswith(suffix) else key
