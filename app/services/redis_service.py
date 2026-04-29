import json
import redis.asyncio as redis

from app.config import settings
from app.services import redis_keys as keys

_pool: redis.Redis | None = None


async def get_redis() -> redis.Redis:
    global _pool
    if _pool is None:
        _pool = redis.from_url(settings.redis_url, decode_responses=True)
    return _pool


# --------------- bloqueio de agente ---------------

async def set_block(phone: str, ttl: int = settings.BLOCK_TTL_SECONDS) -> None:
    r = await get_redis()
    await r.set(keys.block_key(phone), "1", ex=ttl)


async def is_blocked(phone: str) -> bool:
    r = await get_redis()
    return await r.exists(keys.block_key(phone)) == 1


# --------------- buffer de mensagens (debounce) ---------------

async def push_buffer(phone: str, text: str) -> int:
    r = await get_redis()
    return await r.rpush(keys.buffer_key(phone), text)


async def get_buffer(phone: str) -> list[str]:
    r = await get_redis()
    return await r.lrange(keys.buffer_key(phone), 0, -1)


async def delete_buffer(phone: str) -> None:
    r = await get_redis()
    await r.delete(keys.buffer_key(phone))


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
