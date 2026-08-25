import json
import hashlib
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import redis.asyncio as redis

from app.config import settings
from app.services import redis_keys as keys
from app.services.phone_utils import block_variants

logger = logging.getLogger(__name__)

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
#
# TODA operacao de bloqueio varre as variantes com/sem o 9o digito do numero
# (ver app/services/phone_utils.py). O SAI Comercial manda o telefone
# canonicalizado COM o 9; o bot indexa Redis/SQLite pelo JID da UAZAPI, que
# costuma vir SEM o 9. Gravar numa forma e consultar na outra ja fez o bot
# disparar follow-up depois do "Desativar assistente" (o gate de inbound do SAI
# segurava as respostas, mas os jobs proativos consultavam pelo phone do SQLite
# e nao achavam bloqueio nenhum).


def _block_keys(phone: str) -> list[str]:
    return [keys.block_key(v) for v in block_variants(phone)] or [keys.block_key(phone)]


async def set_block(phone: str, ttl: int | None = None, reason: str = "human") -> None:
    r = await get_redis()
    ks = _block_keys(phone)
    # Nunca rebaixa um bloqueio PERMANENTE (botao "Desativar assistente" no SAI,
    # gravado sem TTL) para um bloqueio com prazo. Sem esta guarda, o eco de uma
    # mensagem enviada pela atendente caia aqui e trocava o bloqueio permanente
    # por um que expira amanha 08:00 — o bot voltava a responder no dia seguinte
    # sem ninguem ter reativado o assistente. Basta UMA variante permanente para
    # a guarda valer: as duas formas sao o mesmo lead.
    for key in ks:
        if await r.ttl(key) == -1:  # -1 = existe e nao expira; -2 = nao existe
            return
    seconds = ttl or _block_ttl_seconds()
    for key in ks:
        await r.set(key, reason or "human", ex=seconds)


async def set_permanent_block(phone: str, reason: str = "manual") -> None:
    """Bloqueio sem expiracao (botao 'Desativar assistente' no SAI).
    So sai com clear_block() ou reset_lead_state()."""
    r = await get_redis()
    for key in _block_keys(phone):
        await r.set(key, reason or "manual")


async def clear_block(phone: str) -> bool:
    r = await get_redis()
    removed = 0
    for key in _block_keys(phone):
        removed += await r.delete(key)
    return removed > 0


async def is_blocked(phone: str) -> bool:
    """True se QUALQUER variante do numero estiver bloqueada."""
    r = await get_redis()
    for key in _block_keys(phone):
        if await r.exists(key) == 1:
            return True
    return False


async def is_permanently_blocked(phone: str) -> bool:
    """True se alguma variante tem bloqueio SEM expiracao — ou seja, o operador
    clicou 'Desativar assistente' no SAI. Distingue do bloqueio automatico
    'humano assumiu' (que expira amanha 08:00 SP)."""
    r = await get_redis()
    for key in _block_keys(phone):
        if await r.ttl(key) == -1:
            return True
    return False


async def list_permanent_blocks() -> set[str]:
    """Telefones com bloqueio permanente ativo neste projeto.

    Usado pela reconciliacao com o snapshot do SAI (fonte da verdade): um
    bloqueio permanente local que o SAI nao lista mais foi religado enquanto o
    bot estava fora e precisa cair."""
    r = await get_redis()
    phones: set[str] = set()
    suffix = f"--{settings.PROJECT_SLUG}:block"
    async for key in r.scan_iter(match=f"*{suffix}", count=200):
        if await r.ttl(key) != -1:
            continue  # bloqueio com prazo ("humano assumiu") — nao e do botao
        phones.add(key[: -len(suffix)])
    return phones


async def apply_paused_phones(paused: list[str]) -> tuple[int, int]:
    """Reconcilia os bloqueios permanentes com a lista de conversas pausadas no
    SAI (campo `pausedPhones` do snapshot).

    Fecha os furos que o POST /sai/block sozinho nao cobre: bot fora do ar na
    hora do clique, Redis reiniciado/limpo, bloqueio perdido num redeploy. A
    cada snapshot (push do painel ou poll de 15 min) o estado do SAI volta a
    valer aqui. So mexe em bloqueios SEM prazo — os automaticos de 'humano
    assumiu' seguem intactos.

    Retorna (aplicados, removidos).
    """
    originals = [str(p) for p in (paused or []) if str(p).strip()]
    wanted: set[str] = set()
    for p in originals:
        wanted.update(block_variants(p))

    applied = 0
    for phone in originals:
        if not await is_permanently_blocked(phone):
            await set_permanent_block(phone, reason="manual")
            applied += 1

    removed = 0
    ja_removidos: set[str] = set()
    for phone in await list_permanent_blocks():
        if phone in wanted or phone in ja_removidos:
            continue
        # clear_block apaga as duas formas; marca a irma para nao contar duas vezes.
        ja_removidos.update(block_variants(phone))
        await clear_block(phone)
        removed += 1

    if applied or removed:
        logger.info(
            "reconciliacao de pausa com o SAI: %d bloqueio(s) aplicado(s), %d removido(s)",
            applied, removed,
        )
    return applied, removed



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


async def has_chat_history(phone: str) -> bool:
    r = await get_redis()
    return await r.llen(keys.history_key(phone)) > 0


# --------------- gate de espacamento de envios proativos ---------------

async def set_dispatch_gate(ttl_seconds: int) -> None:
    """Arma o gate anti-ban por `ttl_seconds` (aleatorio, definido no caller).
    Nenhum envio proativo (1o contato / reativacao) sai enquanto ativo."""
    r = await get_redis()
    await r.set(keys.dispatch_gate_key(), "1", ex=max(int(ttl_seconds), 1))


async def is_dispatch_gated() -> bool:
    r = await get_redis()
    return await r.exists(keys.dispatch_gate_key()) == 1


# --------------- alerta de atendimento humano ---------------

async def set_alert_sent(phone: str, ttl: int | None = None) -> None:
    r = await get_redis()
    await r.set(keys.alert_key(phone), "1", ex=ttl or settings.ALERT_COOLDOWN_SECONDS)


async def is_alert_sent(phone: str) -> bool:
    r = await get_redis()
    return await r.exists(keys.alert_key(phone)) == 1


# --------------- alerta de interesse (lead segue na busca) ---------------

async def set_interest_alert_sent(phone: str, ttl: int = 86400) -> None:
    """Marca que o alerta de INTERESSE ja foi enviado para este lead.

    TTL longo (24h por padrao): o alerta de interesse e um evento unico por
    lead, nao precisa repetir a cada mensagem em que ele confirma a busca.
    """
    r = await get_redis()
    await r.set(keys.interest_alert_key(phone), "1", ex=ttl)


async def is_interest_alert_sent(phone: str) -> bool:
    r = await get_redis()
    return await r.exists(keys.interest_alert_key(phone)) == 1




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
async def mark_outbound_id(msg_id: str, ttl: int = 600) -> None:
    """Registra o id de uma mensagem enviada pelo proprio bot.

    Permite reconhecer o eco dessa mensagem (reenviado pelo SAI Comercial) por
    id exato, sem depender de track_source/texto.
    """
    if not msg_id:
        return
    r = await get_redis()
    await r.set(keys.outbound_id_key(msg_id), "1", ex=ttl)


async def is_outbound_id(msg_id: str) -> bool:
    if not msg_id:
        return False
    r = await get_redis()
    return await r.exists(keys.outbound_id_key(msg_id)) == 1

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
    Inclui: histórico, buffer, bloqueio humano, flag de alerta, followup ativo,
    lock de followup e o hash do lead."""
    r = await get_redis()
    await r.delete(
        keys.history_key(phone),
        keys.buffer_key(phone),
        keys.alert_key(phone),
        keys.interest_alert_key(phone),
        keys.followup_active_key(phone),
        keys.followup_lock_key(phone),
        keys.lead_key(phone),
    )
    # Bloqueio sai pelas duas variantes do numero — senao o /reset limpava
    # o estado mas deixava para tras o bloqueio gravado na outra forma.
    await clear_block(phone)


# --------------- trava de follow-up (idempotencia) ---------------

async def acquire_followup_lock(phone: str, ttl: int = 3600) -> bool:
    """SETNX por telefone. Retorna True se o caller ganhou a trava (deve
    processar) ou False se outro processo ja esta cuidando deste lead."""
    if not phone:
        return False
    r = await get_redis()
    ok = await r.set(keys.followup_lock_key(phone), "1", nx=True, ex=ttl)
    return bool(ok)


async def release_followup_lock(phone: str) -> None:
    if not phone:
        return
    r = await get_redis()
    await r.delete(keys.followup_lock_key(phone))
