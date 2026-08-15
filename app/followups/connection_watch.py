"""
Vigia de conexao do WhatsApp.

Motivo: em 14/08/2026 a instancia foi deslogada ("logged out from another
device") no meio de uma resposta. A UAZAPI passou a devolver 503, o bot ficou
mudo e ninguem soube por horas — o unico rastro era um traceback no log do
worker. Este job checa periodicamente o estado da instancia e avisa assim que
ela sai do ar.

Canais de alerta (nesta ordem, todos best-effort):
  1. log em ERROR — sempre acontece, e o que o painel/observabilidade le.
  2. Telegram — canal INDEPENDENTE do WhatsApp, o unico que funciona de fato
     quando a propria instancia caiu. Mesmo bot dos chamados do SAI Comercial.
  3. ALERT_PHONE via WhatsApp — so serve para quedas parciais; se a instancia
     estiver deslogada, este envio falha junto (esperado, nao e erro).

Cooldown no Redis evita repetir o alerta a cada rodada enquanto ninguem
reconecta. Ao voltar, envia um aviso de normalizacao e limpa o cooldown.
"""
import logging

from app.config import settings
from app.services import telegram, uazapi
from app.services.redis_service import get_redis

logger = logging.getLogger("followup.connection_watch")

_ALERT_KEY = "uazapi:connection:alerted"
_OK_STATES = {"connected", "open"}


async def _notify_whatsapp(text: str) -> None:
    if not settings.ALERT_PHONE:
        return
    try:
        await uazapi.send_text(settings.ALERT_PHONE, text)
    except Exception as exc:  # noqa: BLE001
        # Esperado quando a propria instancia esta fora — o alerta util e o do Telegram.
        logger.warning("Alerta de conexao nao saiu pelo WhatsApp (canal fora): %s", exc)


async def run() -> None:
    try:
        instance = await uazapi.get_instance_status()
    except Exception as exc:  # noqa: BLE001
        logger.error("Vigia: nao foi possivel consultar o status da instancia: %s", exc)
        return

    status = str(instance.get("status") or "").lower()
    nome = instance.get("name") or settings.PROJECT_SLUG
    redis = await get_redis()
    ja_alertado = await redis.get(_ALERT_KEY)

    if status in _OK_STATES:
        if ja_alertado:
            logger.info("Vigia: instancia %s reconectada (status=%s)", nome, status)
            await telegram.send_alert(
                f"✅ WhatsApp RECONECTADO\n"
                f"Instancia: {nome}\n"
                "O atendimento automatico voltou ao normal."
            )
            await _notify_whatsapp(
                f"✅ WhatsApp de {nome} reconectado. O atendimento automatico voltou ao normal."
            )
            await redis.delete(_ALERT_KEY)
        return

    motivo = instance.get("lastDisconnectReason") or "motivo nao informado"
    desde = instance.get("lastDisconnect") or "?"
    logger.error(
        "Vigia: WhatsApp FORA DO AR — instancia=%s status=%s desde=%s motivo=%s",
        nome, status, desde, motivo,
    )

    if ja_alertado:
        return  # ja avisamos; espera o cooldown expirar para nao spammar

    await telegram.send_alert(
        f"🚨 WhatsApp FORA DO AR\n"
        f"Instancia: {nome}\n"
        f"Status: {status}\n"
        f"Desde: {desde}\n"
        f"Motivo: {motivo}\n\n"
        "Os leads nao estao recebendo resposta. Reconectar o aparelho."
    )
    await _notify_whatsapp(
        f"🚨 WhatsApp FORA DO AR\n"
        f"Instancia: {nome}\n"
        f"Status: {status}\n"
        f"Desde: {desde}\n"
        f"Motivo: {motivo}\n\n"
        "Os leads nao estao recebendo resposta. Reconectar o aparelho."
    )
    await redis.set(_ALERT_KEY, status, ex=settings.CONNECTION_ALERT_COOLDOWN_SECONDS)
