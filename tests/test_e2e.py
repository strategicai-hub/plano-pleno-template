"""
Teste end-to-end real com Redis + RabbitMQ em containers locais.

Cobre o fluxo completo:
    webhook HTTP -> RabbitMQ -> consumer -> (Gemini/UAZAPI mockados) -> Redis

Gemini e UAZAPI sao substituidos por fakes para nao bater em servicos externos,
mas o Redis e o RabbitMQ sao reais (subidos via docker antes de rodar).

Como rodar:
    docker run -d --rm --name plano-test-redis -p 6399:6379 redis:7-alpine
    docker run -d --rm --name plano-test-rabbit -p 5699:5672 rabbitmq:3-alpine
    python -m pytest tests/test_e2e.py -v
"""
import asyncio
import socket

import pytest
from fastapi.testclient import TestClient


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not (_port_open("127.0.0.1", 6399) and _port_open("127.0.0.1", 5699)),
    reason="Requer Redis em 6399 e RabbitMQ em 5699 (ver docstring do arquivo).",
)


@pytest.fixture
async def clean_redis():
    """Limpa o Redis de teste antes e depois do teste."""
    from app.services.redis_service import get_redis
    r = await get_redis()
    await r.flushdb()
    yield r
    await r.flushdb()


@pytest.fixture(autouse=True)
async def reset_connections_between_tests():
    """
    pytest-asyncio cria um novo event loop por teste. Os caches de conexao
    (RabbitMQ channel, Redis pool) estao amarrados ao loop anterior, entao
    precisamos zerar antes e depois de cada teste.
    """
    from app.services import rabbitmq
    from app.services import redis_service

    rabbitmq._connection = None
    rabbitmq._channel = None
    redis_service._pool = None
    yield
    rabbitmq._connection = None
    rabbitmq._channel = None
    redis_service._pool = None


@pytest.mark.asyncio
async def test_full_flow_webhook_to_whatsapp(monkeypatch, clean_redis):
    """
    Envia um POST no webhook, espera o consumer processar via RabbitMQ real,
    e verifica que: (a) o Gemini mock foi chamado, (b) a resposta foi enviada
    via UAZAPI mock, (c) historico + lead apareceram no Redis real.
    """
    import app.main as main_mod
    from app import consumer as consumer_mod
    from app.services import gemini as gemini_mod, uazapi as uazapi_mod

    gemini_calls: list[tuple[str, str]] = []
    uazapi_calls: list[tuple[str, str]] = []

    async def fake_gemini_chat(phone: str, user_message: str, lead_name: str = ""):
        gemini_calls.append((phone, user_message))
        # Grava o historico no Redis usando a funcao real, para testar esse caminho.
        from app.services.redis_service import append_chat_history
        await append_chat_history(phone, "user", user_message)
        ai_text = "Ola! Como posso ajudar? [TRANSFERIR=0]"
        await append_chat_history(phone, "model", ai_text)
        return ai_text, (10, 20, 30)

    async def fake_send_text(number: str, text: str, delay: int = 4000):
        uazapi_calls.append((number, text))
        return {"status": "sent"}

    # Mocks: consumer.py usa gemini_chat importado direto, precisamos monkey no consumer
    monkeypatch.setattr(consumer_mod, "gemini_chat", fake_gemini_chat)
    monkeypatch.setattr(uazapi_mod, "send_text", fake_send_text)
    # Remove delay de debounce para o teste rodar rapido
    from app.config import settings
    settings.DEBOUNCE_SECONDS = 0

    # Sobe o consumer em background
    consumer_task = asyncio.create_task(consumer_mod.start_consumer())

    try:
        # POST webhook via TestClient (sincrono, em threadpool)
        phone = "5511999990001"
        client = TestClient(main_mod.app)
        r = client.post("/testslug", json={
            "message": {
                "sender_pn": f"{phone}@c.us",
                "senderName": "Teste Lead",
                "text": "quero informacoes da academia",
            }
        })
        assert r.status_code == 200
        assert r.json() == {"status": "queued"}

        # Aguarda o consumer processar (Gemini mock + UAZAPI mock rodam)
        for _ in range(50):  # ate 10s
            if uazapi_calls:
                break
            await asyncio.sleep(0.2)

        assert len(gemini_calls) == 1, f"Gemini nao foi chamado: {gemini_calls}"
        assert gemini_calls[0][0] == phone
        assert "quero informacoes" in gemini_calls[0][1]

        assert len(uazapi_calls) == 1, f"UAZAPI nao foi chamado: {uazapi_calls}"
        sent_to, sent_text = uazapi_calls[0]
        assert sent_to == phone
        assert "Como posso ajudar" in sent_text
        # Flag nao deve vazar para o WhatsApp
        assert "[TRANSFERIR" not in sent_text

        # Verifica Redis real: lead + historico
        from app.services import redis_keys as keys
        lead = await clean_redis.hgetall(keys.lead_key(phone))
        assert lead.get("name") == "Teste Lead"
        history = await clean_redis.lrange(keys.history_key(phone), 0, -1)
        assert len(history) == 2  # human + ai
        assert "quero informacoes" in history[0]
        assert "Como posso ajudar" in history[1]

    finally:
        consumer_task.cancel()
        try:
            await consumer_task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_blocked_lead_is_ignored(monkeypatch, clean_redis):
    """Se o lead esta bloqueado, o consumer nao chama Gemini nem envia mensagem."""
    import app.main as main_mod
    from app import consumer as consumer_mod
    from app.services import uazapi as uazapi_mod
    from app.services.redis_service import set_block

    gemini_calls: list = []
    uazapi_calls: list = []

    async def fake_gemini(*a, **kw):
        gemini_calls.append(a)
        return "nao deveria ter sido chamado", (0, 0, 0)

    async def fake_send_text(*a, **kw):
        uazapi_calls.append(a)
        return {}

    monkeypatch.setattr(consumer_mod, "gemini_chat", fake_gemini)
    monkeypatch.setattr(uazapi_mod, "send_text", fake_send_text)
    from app.config import settings
    settings.DEBOUNCE_SECONDS = 0

    phone = "5511999990002"
    await set_block(phone, ttl=60)

    consumer_task = asyncio.create_task(consumer_mod.start_consumer())
    try:
        client = TestClient(main_mod.app)
        client.post("/testslug", json={
            "message": {
                "sender_pn": f"{phone}@c.us",
                "text": "oi",
            }
        })
        # Espera tempo suficiente para o consumer teoricamente ter processado
        await asyncio.sleep(2)
        assert gemini_calls == []
        assert uazapi_calls == []
    finally:
        consumer_task.cancel()
        try:
            await consumer_task
        except (asyncio.CancelledError, Exception):
            pass
