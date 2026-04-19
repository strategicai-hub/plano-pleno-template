"""
Utilitarios de distribuicao temporal de envios em lote.

Jobs de follow-up disparados uma vez por janela tendem a enviar todas as
mensagens em sequencia no mesmo minuto. Para diluir a carga no WhatsApp /
UAZAPI e parecer mais humano, distribuimos cada envio em um horario aleatorio
dentro de uma janela configuravel.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, Sequence, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def distribute_over_window(
    items: Sequence[T],
    send_fn: Callable[[T], Awaitable[None]],
    window_seconds: int = 3600,
    label: str = "envios",
) -> None:
    """
    Executa send_fn(item) para cada item, espalhando os envios na janela.
    Cada item recebe offset aleatorio uniforme em [0, window_seconds).
    Exceptions em send_fn sao logadas e o loop continua.
    """
    if not items:
        return

    offsets = sorted(
        (random.uniform(0, window_seconds), idx, item)
        for idx, item in enumerate(items)
    )
    logger.info(
        "%s: distribuindo %d envio(s) em janela de %ds",
        label, len(offsets), window_seconds,
    )

    start = asyncio.get_event_loop().time()
    for offset, idx, item in offsets:
        elapsed = asyncio.get_event_loop().time() - start
        wait = max(0.0, offset - elapsed)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            await send_fn(item)
        except Exception:
            logger.exception("%s: falha enviando item #%d", label, idx)
