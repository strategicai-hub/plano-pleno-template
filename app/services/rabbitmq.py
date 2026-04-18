import asyncio
import json
import logging
from typing import Callable, Awaitable

import aio_pika

from app.config import settings

logger = logging.getLogger(__name__)

_connection: aio_pika.abc.AbstractRobustConnection | None = None
_channel: aio_pika.abc.AbstractChannel | None = None
_lock = asyncio.Lock()


async def _get_channel() -> aio_pika.abc.AbstractChannel:
    """Reaproveita conexao e canal entre publishes.

    aio_pika.connect_robust ja reconecta sozinho em caso de drop; so precisamos
    abrir uma unica vez. O lock evita que dois publishes simultaneos no startup
    abram duas conexoes.
    """
    global _connection, _channel
    if _channel is not None and not _channel.is_closed:
        return _channel
    async with _lock:
        if _channel is not None and not _channel.is_closed:
            return _channel
        _connection = await aio_pika.connect_robust(settings.rabbitmq_url)
        _channel = await _connection.channel()
        await _channel.declare_queue(settings.RABBITMQ_QUEUE, durable=True)
        return _channel


async def publish(message: dict) -> None:
    channel = await _get_channel()
    await channel.default_exchange.publish(
        aio_pika.Message(
            body=json.dumps(message).encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        ),
        routing_key=settings.RABBITMQ_QUEUE,
    )
    logger.info("Mensagem publicada na fila %s", settings.RABBITMQ_QUEUE)


async def close() -> None:
    global _connection, _channel
    if _channel is not None and not _channel.is_closed:
        await _channel.close()
    if _connection is not None and not _connection.is_closed:
        await _connection.close()
    _channel = None
    _connection = None


async def consume(callback: Callable[[dict], Awaitable[None]]) -> None:
    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    channel = await connection.channel()
    await channel.set_qos(prefetch_count=1)
    queue = await channel.declare_queue(settings.RABBITMQ_QUEUE, durable=True)

    logger.info("Consumindo fila %s ...", settings.RABBITMQ_QUEUE)

    async with queue.iterator() as queue_iter:
        async for message in queue_iter:
            async with message.process():
                try:
                    body = json.loads(message.body.decode())
                    await callback(body)
                except Exception:
                    logger.exception("Erro ao processar mensagem da fila")
