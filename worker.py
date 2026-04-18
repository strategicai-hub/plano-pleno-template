"""
Worker: consome mensagens do RabbitMQ, processa com IA e responde via UAZAPI.
Entrypoint separado da API para deploy independente no Portainer.
"""
import asyncio
import logging

from app.consumer import start_consumer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

if __name__ == "__main__":
    asyncio.run(start_consumer())
