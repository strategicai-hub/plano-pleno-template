import json
import hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import redis.asyncio as redis

from app.config import settings
from app.services import redis_keys as keys

_pool: redis.Redis | None = None


async def get_redis() -> redis.Redis:
    global _pool
    if _pool is None:
        _pool = redis.from_url(settings.redis_url, decode_responses=True)
    return _pool


def _block_ttl_seconds() -> int:
    # Bloqueio expira amanhã às 08:00 SP — bot só volta no dia seguinte.
    tz = ZoneInfo("America/Sao_Paulo")
    now = datetime.now(tz)
    target = (now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    return max(int((target - now).total_seconds()), 60)


# --------------- bloqueio de agente ---------------

async def set_block(phone: str, ttl: int | None = None, reason: str = "human") -> None:
    r = await get_redis()
    await r.set(keys.block_key(phone), reason or "human", ex=ttl or _block_ttl_seconds())


async def is_blocked(phone: str) -> bool:
    r = await get_redis()
    return await r.exists(keys.block_key(phone)) == 1



async def clear_stale_legacy_block(phone: str) -> bool:
    """Remove bloqueio antigo deixado por eco do /reset.

    Versoes anteriores gravavam o bloqueio como "1". Se o reset apagou lead,
    historico e buffer, mas um eco outbound criou esse bloqueio legado logo
    depois, a proxima mensagem do lead nao pode ficar presa ate o dia seguinte.
    """
    r = await get_redis()
    block_key = keys.block_key(phone)
    value = await r.get(block_key)
    if value != "1":
        return False

    has_history = await r.llen(keys.history_key(phone)) > 0
    has_lead = await r.exists(keys.lead_key(phone)) == 1
    has_buffer = await r.exists(keys.buffer_key(phone)) == 1
    if has_history or has_lead or has_buffer:
        return False

    await r.delete(block_key)
    return True


# --------------- buffer de mensagens (debounce) ---------------

def _buffer_ttl_seconds() -> int:
    """TTL do buffer de debounce.

    Garante autolimpeza se a task que deveria consumir o buffer morrer (ex.:
    redeploy/restart do worker durante o sleep do debounce, exceção no Gemini,
    blip no Redis). Sem isso, o buffer ficaria pendurado e toda mensagem
    seguinte do lead veria count>1 e sairia calada — bot mudo permanente.
    Folga de 60s sobre a janela de debounce, mínimo 90s.
    """
    return max(int(settings.DEBOUNCE_SECONDS) + 60, 90)


async def push_buffer(phone: str, text: str) -> int:
    r = await get_redis()
    key = keys.buffer_key(phone)
    async with r.pipeline(transaction=True) as pipe:
        pipe.rpush(key, text)
        pipe.expire(key, _buffer_ttl_seconds())
        results = await pipe.execute()
    return results[0]  # tamanho da lista após o rpush


async def pop_buffer(phone: str) -> list[str]:
    """Lê e apaga o buffer atomicamente (LRANGE + DELETE em MULTI/EXEC).

    Elimina a janela de corrida do antigo get_buffer()+delete_buffer(): sem
    isso, uma terceira mensagem podia chegar entre o get e o delete, recriar o
    buffer com count=1 e disparar um reprocessamento duplicado.
    """
    r = await get_redis()
    key = keys.buffer_key(phone)
    async with r.pipeline(transaction=True) as pipe:
        pipe.lrange(key, 0, -1)
        pipe.delete(key)
        results = await pipe.execute()
    return results[0] or []


async def scan_buffer_phones() -> list[str]:
    """Lista os phones que têm buffer de debounce pendente no Redis.

    Usado na recuperação de buffers órfãos no startup do worker: mensagens cuja
    task de debounce foi interrompida por redeploy/restart deixam o buffer no
    Redis (com TTL), mas ninguém as reprocessaria — esta varredura recupera."""
    r = await get_redis()
    phones: list[str] = []
    async for key in r.scan_iter(match=keys.buffer_scan_pattern(), count=100):
        phones.append(keys.phone_from_buffer_key(key))
    return phones


async def get_buffer(phone: str) -> list[str]:
    r = await get_redis()
    return await r.lrange(keys.buffer_key(phone), 0, -1)


async def delete_buffer(phone: str) -> None:
    r = await get_redis()
    await r.delete(keys.buffer_key(phone))


async def mark_processed(message_id: str, ttl: int = 300) -> bool:
    """Marca um message_id como processado. Retorna True se foi marcado agora
    (primeira vez), False se já existia (reentrega da UAZAPI / retry HTTP).

    Usa SET NX para fechar a corrida entre duas entregas simultâneas do mesmo
    evento.
    """
    if not message_id:
        return True
    r = await get_redis()
    ok = await r.set(keys.processed_key(message_id), "1", ex=ttl, nx=True)
    return bool(ok)


# --------------- historico de chat (Gemini) ---------------

async def get_chat_history(phone: str) -> list[dict]:
    r = await get_redis()
    raw = await r.lrange(keys.history_key(phone), 0, -1)
    history = []
    for item in raw:
        entry = json.loads(item)
        if "type" in entry:
            # Formato novo: {"type": "ai"/"human", "data": {"content": "..."}}
            role = "model" if entry["type"] == "ai" else "user"
            text = entry.get("data", {}).get("content", "")
            history.append({"role": role, "parts": [{"text": text}]})
        else:
            # Formato legado: passa direto para o Gemini
            history.append(entry)
    return history


async def append_chat_history(phone: str, role: str, text: str) -> None:
    r = await get_redis()
    entry_type = "ai" if role == "model" else "human"
    entry = json.dumps({"type": entry_type, "data": {"content": text}}, ensure_ascii=False)
    await r.rpush(keys.history_key(phone), entry)
    await r.ltrim(keys.history_key(phone), -50, -1)  # manter ultimas 50 msgs


async def clear_chat_history(phone: str) -> None:
    r = await get_redis()
    await r.delete(keys.history_key(phone))


# --------------- alerta de atendimento humano ---------------

async def set_alert_sent(phone: str, ttl: int | None = None) -> None:
    r = await get_redis()
    await r.set(keys.alert_key(phone), "1", ex=ttl or settings.ALERT_COOLDOWN_SECONDS)


async def is_alert_sent(phone: str) -> bool:
    r = await get_redis()
    return await r.exists(keys.alert_key(phone)) == 1




# --------------- ecos de mensagens enviadas pela propria API ---------------

def _outbound_digest(text: str) -> str:
    normalized = (text or "").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


async def mark_outbound_echo(phone: str, text: str, ttl: int = 120) -> None:
    if not phone or not text:
        return
    r = await get_redis()
    await r.set(keys.outbound_echo_key(phone, _outbound_digest(text)), "1", ex=ttl)


async def consume_outbound_echo(phone: str, text: str) -> bool:
    if not phone or not text:
        return False
    r = await get_redis()
    key = keys.outbound_echo_key(phone, _outbound_digest(text))
    deleted = await r.delete(key)
    return deleted == 1

# --------------- leads ---------------

async def get_lead(phone: str) -> dict | None:
    r = await get_redis()
    data = await r.hgetall(keys.lead_key(phone))
    return data if data else None


async def create_lead(phone: str, name: str = "") -> dict:
    r = await get_redis()
    lead = {
        "phone": phone,
        "name": name,
        "status_conversa": "Novo",
        "created_at": "",
    }
    await r.hset(keys.lead_key(phone), mapping=lead)
    return lead


async def update_lead(phone: str, **fields) -> None:
    r = await get_redis()
    if fields:
        await r.hset(keys.lead_key(phone), mapping=fields)


async def delete_lead(phone: str) -> None:
    r = await get_redis()
    await r.delete(keys.lead_key(phone))


async def reset_lead_state(phone: str) -> None:
    """Apaga TODAS as chaves Redis relacionadas ao lead — usado pelo /reset.
    Inclui: histórico, buffer, bloqueio humano, flag de alerta, followup ativo
    e o hash do lead."""
    r = await get_redis()
    await r.delete(
        keys.history_key(phone),
        keys.buffer_key(phone),
        keys.block_key(phone),
        keys.alert_key(phone),
        keys.followup_active_key(phone),
        keys.lead_key(phone),
    )
