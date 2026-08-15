"""
Alertas operacionais por Telegram.

Por que Telegram e nao WhatsApp: quando a instancia UAZAPI cai, o alerta por
WhatsApp cai junto — foi exatamente o que aconteceu em 14/08/2026, quando a
instancia da dra-mariana-maia ficou horas deslogada sem ninguem saber. O
Telegram e o unico canal independente do proprio canal que estamos monitorando.

Usa o MESMO bot que notifica chamados abertos no SAI Comercial
(sai-comercial/src/lib/chamados/telegram.ts) — sem bot novo para manter.

Origem das credenciais, nesta ordem:
  1. env TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (quando a stack define);
  2. chaves globais no Redis (`sai:telegram:bot_token` / `sai:telegram:chat_id`).

O fallback pelo Redis existe porque as stacks dos bots sao majoritariamente do
tipo Git no Portainer, e alterar env var dessas stacks exige PUT na config Git —
operacao que apaga a credencial do repo privado e quebra o deploy seguinte. Com
o Redis, o token e configurado num lugar so, sem tocar em stack nenhuma.

Silencioso quando nao configurado e nunca levanta excecao — alerta jamais
derruba o fluxo que o chamou.
"""
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0
_TOKEN_KEY = "sai:telegram:bot_token"
_CHAT_KEY = "sai:telegram:chat_id"

# Cache em processo: as chaves nao mudam em runtime e o alerta pode disparar
# dentro do fluxo de resposta ao lead — nao vale um round-trip por alerta.
_cache: tuple[str, str] | None = None


async def _credentials() -> tuple[str, str]:
    global _cache
    if _cache is not None:
        return _cache
    token = (settings.TELEGRAM_BOT_TOKEN or "").strip()
    chat = (str(settings.TELEGRAM_CHAT_ID or "")).strip()
    if not (token and chat):
        try:
            from app.services.redis_service import get_redis

            redis = await get_redis()
            token = token or (await redis.get(_TOKEN_KEY) or "").strip()
            chat = chat or (await redis.get(_CHAT_KEY) or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Nao consegui ler credenciais do Telegram no Redis: %s", exc)
    _cache = (token, chat)
    return _cache


def _prefix() -> str:
    """Identifica de qual bot veio o alerta — o chat e compartilhado pela frota."""
    return (settings.PROJECT_SLUG or "chatbot").strip()


async def send_alert(text: str) -> bool:
    """Envia alerta. Retorna True se entregou. Nunca levanta."""
    token, chat_id = await _credentials()
    if not (token and chat_id):
        logger.debug("Telegram nao configurado — alerta ignorado: %s", text[:120])
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": f"[{_prefix()}]\n{text}",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001 - alerta e best-effort por definicao
        logger.warning("Falha ao enviar alerta pelo Telegram: %s", exc)
        return False
